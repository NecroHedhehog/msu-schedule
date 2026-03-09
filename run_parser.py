#!/usr/bin/env python3
"""
Точка запуска парсера расписания.

Использование:
    python run_parser.py              — парсить все (группы + студенты)
    python run_parser.py socio        — только групповые расписания
    python run_parser.py students     — только студенты + преподаватели
    python run_parser.py --test       — тест на локальном HTML файле
"""

import sys
from core.database import (
    get_connection, get_or_create_faculty, get_or_create_group,
    save_lessons, log_parse
)
from core.alerts import alert_parse_ok, alert_parse_error, alert_parse_warning

# Импорт функций для студентов
from core.db_students import (
    get_groups_for_student_parse, save_students, update_lesson_teachers,
    ensure_tables, fill_teachers_from_same_subject,
    ensure_student_subjects_table, save_student_subjects,
)
MIN_EXPECTED_LESSONS = 50


def run_socio():
    """Парсинг групповых расписаний соцфака."""
    from parsers.socio import SocioParser

    parser = SocioParser()

    try:
        result = parser.parse()
    except Exception as e:
        alert_parse_error('socio', f"Исключение: {e}")
        conn = get_connection()
        log_parse(conn, 'socio', 'error', message=str(e))
        conn.close()
        return

    if not result['groups']:
        alert_parse_error('socio', 'Парсер вернул 0 групп.')
        conn = get_connection()
        log_parse(conn, 'socio', 'error', message='Нет данных')
        conn.close()
        return

    conn = get_connection()
    faculty_id = get_or_create_faculty(
        conn, code=parser.FACULTY_CODE, name=parser.FACULTY_NAME, domain=parser.DOMAIN,
    )

    total_lessons = 0
    for group_data in result['groups']:
        group_id = get_or_create_group(
            conn, faculty_id=faculty_id, code=group_data['code'],
            site_id=group_data.get('site_id', ''),
            department=group_data.get('department', ''),
            program=group_data.get('program', ''),
        )
        save_lessons(conn, group_id, group_data['lessons'])
        total_lessons += len(group_data['lessons'])

    log_parse(conn, 'socio', 'ok', lessons_count=total_lessons, groups_count=len(result['groups']))
    conn.close()

    print(f"\n[socio] Сохранено: {len(result['groups'])} групп, {total_lessons} занятий")

    if total_lessons < MIN_EXPECTED_LESSONS:
        alert_parse_warning('socio', f"Мало данных: {total_lessons} занятий")
    else:
        alert_parse_ok('socio', len(result['groups']), total_lessons)


def run_students():
    """Парсинг студентов и преподавателей соцфака."""
    from parsers.socio import SocioParser

    conn = get_connection()
    ensure_tables(conn)
    groups = get_groups_for_student_parse(conn)
    conn.close()

    if not groups:
        print("[students] Нет групп в базе. Сначала запусти: python run_parser.py socio")
        return

    # Фильтр по курсу/коду
    filter_arg = None
    for a in sys.argv:
        if a.startswith('--filter='):
            filter_arg = a.split('=', 1)[1].lower()

    if filter_arg:
        groups_info = [(g['id'], g['code'], g['site_id']) for g in groups
                       if filter_arg in g['code'].lower()]
        print(f"[students] Фильтр '{filter_arg}': {len(groups_info)} групп из {len(groups)}")
    else:
        groups_info = [(g['id'], g['code'], g['site_id']) for g in groups]

    print(f"[students] Групп: {len(groups_info)}")

    parser = SocioParser()

    try:
        result = parser.parse_students(groups_info)
    except Exception as e:
        alert_parse_error('socio-students', f"Исключение: {e}")
        print(f"[students] Ошибка: {e}")
        import traceback
        traceback.print_exc()
        return

    conn = get_connection()
    ensure_student_subjects_table(conn)
    students_by_group = {}

    for s in result['students']:
        gid = s['group_id']
        if gid not in students_by_group:
            students_by_group[gid] = []
        students_by_group[gid].append(s)

    total_students = 0
    for gid, students in students_by_group.items():
        save_students(conn, gid, students)
        # Сохранить предметы каждого студента
        for s in students:
            if s.get('subjects'):
                # Найти student_id в базе по site_id
                row = conn.execute(
                    "SELECT id FROM students WHERE group_id = ? AND site_id = ?",
                    (gid, s['site_id'])
                ).fetchone()
                if row:
                    save_student_subjects(conn, row['id'], s['subjects'])
        total_students += len(students)

    updated = update_lesson_teachers(conn, result['teacher_updates'])
    conn.close()

    print(f"\n[students] Сохранено: {total_students} студентов, "
          f"обновлено преподавателей: {updated} занятий")
    alert_parse_ok('socio-students', total_students, updated)


def run_teachers():
    """Парсинг преподавателей через кафедры (заполняет пробелы)."""
    from parsers.socio import SocioParser

    conn = get_connection()
    groups = get_groups_for_student_parse(conn)
    conn.close()

    if not groups:
        print("[teachers] Нет групп в базе.")
        return

    group_code_to_id = {g['code']: g['id'] for g in groups}
    print(f"[teachers] Групп в маппинге: {len(group_code_to_id)}")

    parser = SocioParser()

    try:
        result = parser.parse_teachers(group_code_to_id)
    except Exception as e:
        alert_parse_error('socio-teachers', f"Исключение: {e}")
        print(f"[teachers] Ошибка: {e}")
        import traceback
        traceback.print_exc()
        return
    conn = get_connection()
    updated = update_lesson_teachers(conn, result['teacher_updates'])
    filled = fill_teachers_from_same_subject(conn)
    conn.close()

    print(f"[teachers] Обновлено: {updated} занятий")
    print(f"[teachers] Дозаполнено из лекций→семинаров: {filled}")
    alert_parse_ok('socio-teachers', result['teachers_found'], updated)


def run_test():
    """Тест парсера на локальном HTML файле."""
    from parsers.socio import SocioParser

    filepath = sys.argv[2] if len(sys.argv) > 2 else 'socio.html'

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            html = f.read()
    except FileNotFoundError:
        print(f"Файл не найден: {filepath}")
        return

    parser = SocioParser()
    lessons = parser._parse_page(html)

    print(f"\nРезультат: {len(lessons)} занятий\n")

    from collections import defaultdict
    by_date = defaultdict(list)
    for l in lessons:
        by_date[l['date']].append(l)

    for dt in sorted(by_date.keys()):
        entries = sorted(by_date[dt], key=lambda x: (x['pair_number'], x['subject_abbr']))
        print(f"  {dt}")
        for e in entries:
            teacher = f" | {e['teacher']}" if e['teacher'] else ""
            print(f"   {e['pair_number']} пара ({e['time_start']}-{e['time_end']}) "
                  f"| {e['subject_abbr']:10s} | ауд.{e['room']:4s} "
                  f"[{e['lesson_type']:3s}]{teacher}")
        print()


def main():
    args = sys.argv[1:]

    if '--test' in args:
        run_test()
    elif 'teachers' in args:
        run_teachers()
    elif 'students' in args:
        run_students()
    elif 'socio' in args:
        run_socio()
    elif not args:
        run_socio()
        print("\n" + "=" * 60 + "\n")
        run_students()
        print("\n" + "=" * 60 + "\n")
        run_teachers()
    else:
        print("Использование:")
        print("  python run_parser.py              — всё (группы + студенты + преподаватели)")
        print("  python run_parser.py socio        — только групповые расписания")
        print("  python run_parser.py students     — студенты + преподаватели из расписаний")
        print("  python run_parser.py teachers     — преподаватели через кафедры (заполняет пробелы)")
        print("  python run_parser.py --test       — тест на локальном файле")


if __name__ == '__main__':
    main()
