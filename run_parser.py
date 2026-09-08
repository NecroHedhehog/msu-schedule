#!/usr/bin/env python3
"""
Точка запуска парсера расписания.

Использование:
    python run_parser.py                      — всё (группы + студенты + преподаватели)
    python run_parser.py socio                — только групповые расписания (~107 запросов)
    python run_parser.py subgroups            — языковые потоки (~350 запросов)
    python run_parser.py students             — студенты + преподаватели из расписаний
    python run_parser.py students --resume    — продолжить прерванный прогон
    python run_parser.py students --filter=с4 — только группы, чей код содержит 'с4'
    python run_parser.py teachers             — преподаватели через кафедры (~586 запросов)
    python run_parser.py --test [файл]        — тест на локальном HTML

Сайт факультета регулярно недоступен, поэтому сохранение идёт по мере
продвижения: обрыв на середине не обнуляет уже собранное, а в parse_log
пишется статус 'partial' — то есть данные есть, но неполные.
"""

import sys

from core.config import MIN_LESSONS_PER_GROUP
from core.database import (
    get_connection, get_or_create_faculty, get_or_create_group,
    save_lessons, save_subgroup_lessons, log_parse
)
from core.alerts import alert_parse_ok, alert_parse_error, alert_parse_warning

from core.db_students import (
    get_groups_for_student_parse, save_students, update_lesson_teachers,
    ensure_tables, fill_teachers_from_same_subject,
    ensure_student_subjects_table, save_student_subjects,
    group_subject_coverage,
)

# Порог по факультету: если суммарно меньше — прогон явно провалился
MIN_EXPECTED_LESSONS = 50

# Доля студентов группы с собранными предметами, при которой --resume
# считает группу уже собранной
RESUME_COVERAGE = 0.8


# ======= Разбор аргументов =======

def _has_flag(name: str) -> bool:
    return name in sys.argv[1:]


def _get_opt(prefix: str):
    for a in sys.argv[1:]:
        if a.startswith(prefix):
            return a[len(prefix):]
    return None


def _report(tag: str, parser, problems: list):
    """Печать итогов прогона в одном месте."""
    print(f"[{tag}] {parser.stats_line()}")
    if parser.failed_urls:
        print(f"[{tag}] Неудачные запросы (первые {len(parser.failed_urls)}):")
        for u in parser.failed_urls[:10]:
            print("   ", u)
    if parser.unparsed_samples:
        print(f"[{tag}] Примеры нераспознанных блоков:")
        for sample in parser.unparsed_samples:
            print("   ", sample)
    for p in problems:
        print(f"[{tag}] ! {p}")


def _counter_problems(parser) -> list:
    """
    Поводы для статуса 'warning', видимые прямо в счётчиках.
    В message их не дублируем — они уже есть в stats_line().
    """
    problems = []
    if parser.requests_failed:
        problems.append(f"неудачных запросов: {parser.requests_failed}")
    if parser.unparsed_blocks:
        problems.append(f"нераспознанных блоков: {parser.unparsed_blocks}")
    return problems


def _compose(parser, extra: list) -> tuple:
    """(статус, сообщение для parse_log, полный список проблем)."""
    problems = _counter_problems(parser) + extra
    message = parser.stats_line()
    if extra:
        message += ' | ' + ' | '.join(extra)
    return ('warning' if problems else 'ok'), message, problems


# ======= Групповые расписания =======

def run_socio():
    """Парсинг групповых расписаний соцфака. Сохраняет после каждой группы."""
    from parsers.socio import SocioParser

    parser = SocioParser()

    conn = get_connection()
    faculty_id = get_or_create_faculty(
        conn, code=parser.FACULTY_CODE, name=parser.FACULTY_NAME, domain=parser.DOMAIN,
    )

    saved_groups = 0
    saved_lessons = 0
    thin = []       # группы с подозрительно малым числом занятий
    guarded = []    # группы, где сработала защита от затирания

    def on_group(g):
        nonlocal saved_groups, saved_lessons
        group_id = get_or_create_group(
            conn, faculty_id=faculty_id, code=g['code'],
            site_id=g.get('site_id', ''),
            department=g.get('department', ''),
            program=g.get('program', ''),
        )
        res = save_lessons(conn, group_id, g['lessons'])
        saved_groups += 1
        saved_lessons += res['written']

        if res['skipped']:
            guarded.append(f"{g['code']} ({res['reason']})")
            print(f"        {g['code']}: НЕ ЗАПИСАНО — {res['reason']}")
        elif len(g['lessons']) < MIN_LESSONS_PER_GROUP:
            # Порог на группу, а не на факультет: раньше семь пустых
            # магистерских групп проходили как 'ok', потому что 5162 > 50
            thin.append(f"{g['code']}={len(g['lessons'])}")

    try:
        parser.parse(on_group=on_group)
    except Exception as e:
        message = f"{type(e).__name__}: {e} | {parser.stats_line()}"
        log_parse(conn, 'socio', 'partial', lessons_count=saved_lessons,
                  groups_count=saved_groups, message=message)
        conn.close()
        print(f"\n[socio] Прогон оборван: {e}")
        print(f"[socio] Успели сохранить: {saved_groups} групп, {saved_lessons} занятий")
        alert_parse_error(
            'socio',
            f"Прогон оборван: {e}\n"
            f"Сохранено до обрыва: {saved_groups} групп / {saved_lessons} занятий"
        )
        return

    extra = []
    if guarded:
        extra.append(f"защита от затирания у {len(guarded)} групп: " + ', '.join(guarded[:5]))
    if thin:
        extra.append(f"мало занятий у {len(thin)} групп: " + ', '.join(thin[:10]))

    status, message, problems = _compose(parser, extra)

    log_parse(conn, 'socio', status, lessons_count=saved_lessons,
              groups_count=saved_groups, message=message)
    conn.close()

    print(f"\n[socio] Сохранено: {saved_groups} групп, {saved_lessons} занятий")
    _report('socio', parser, problems)

    if saved_lessons < MIN_EXPECTED_LESSONS:
        alert_parse_error('socio', f"Мало данных: {saved_lessons} занятий")
    elif problems:
        alert_parse_warning('socio', '\n'.join(problems))
    else:
        alert_parse_ok('socio', saved_groups, saved_lessons)


# ======= Подгруппы: языковые потоки =======

def run_subgroups():
    """
    Расписания подгрупп — потоков иностранного языка.

    Сайт заводит их отдельными сущностями, потому что группа учит разные
    языки и одним занятием на всю группу это не показать. В групповом
    расписании таких занятий нет вовсе.

    Запускать ПОСЛЕ socio: сохраняются только занятия, которых у группы нет,
    а для этого расписание группы должно уже лежать в базе.
    """
    from parsers.socio import SocioParser

    conn = get_connection()
    groups = get_groups_for_student_parse(conn)

    if not groups:
        print("[subgroups] Нет групп в базе. Сначала: python run_parser.py socio")
        conn.close()
        return

    groups_info = [(g['id'], g['code'], g['site_id']) for g in groups]
    print(f"[subgroups] Групп: {len(groups_info)}")

    saved_lessons = 0
    saved_subgroups = 0

    def on_subgroup(group_id, label, lessons):
        nonlocal saved_lessons, saved_subgroups
        written = save_subgroup_lessons(conn, group_id, label, lessons)
        saved_lessons += written
        if written:
            saved_subgroups += 1
        return written

    parser = SocioParser()

    try:
        result = parser.parse_subgroups(groups_info, on_subgroup=on_subgroup)
    except Exception as e:
        message = f"{type(e).__name__}: {e} | {parser.stats_line()}"
        log_parse(conn, 'socio-subgroups', 'partial',
                  lessons_count=saved_lessons, groups_count=saved_subgroups,
                  message=message)
        conn.close()
        print(f"\n[subgroups] Прогон оборван: {e}")
        print(f"[subgroups] Сохранено до обрыва: {saved_lessons} занятий")
        alert_parse_error('socio-subgroups',
                          f"Прогон оборван: {e}\n"
                          f"Сохранено до обрыва: {saved_lessons} занятий")
        return

    status, message, problems = _compose(parser, [])
    log_parse(conn, 'socio-subgroups', status,
              lessons_count=saved_lessons, groups_count=saved_subgroups,
              message=message)
    conn.close()

    print(f"\n[subgroups] Подгрупп с собственными занятиями: {saved_subgroups} "
          f"из {result['subgroups']}")
    print(f"[subgroups] Записано занятий, которых нет у группы: {saved_lessons}")
    _report('subgroups', parser, problems)

    if problems:
        alert_parse_warning('socio-subgroups', '\n'.join(problems))
    else:
        alert_parse_ok('socio-subgroups', saved_subgroups, saved_lessons)


# ======= Студенты и преподаватели из персональных расписаний =======

def run_students():
    """
    Парсинг студентов и преподавателей соцфака.
    Сохраняет после каждой группы, умеет продолжать с места обрыва (--resume).
    """
    from parsers.socio import SocioParser

    conn = get_connection()
    ensure_tables(conn)
    ensure_student_subjects_table(conn)
    groups = get_groups_for_student_parse(conn)

    if not groups:
        print("[students] Нет групп в базе. Сначала запусти: python run_parser.py socio")
        conn.close()
        return

    filter_arg = _get_opt('--filter=')
    if filter_arg:
        filter_arg = filter_arg.lower()
        groups_info = [(g['id'], g['code'], g['site_id']) for g in groups
                       if filter_arg in g['code'].lower()]
        print(f"[students] Фильтр '{filter_arg}': {len(groups_info)} групп из {len(groups)}")
    else:
        groups_info = [(g['id'], g['code'], g['site_id']) for g in groups]

    resume = _has_flag('--resume')
    if resume:
        print("[students] Режим --resume: уже собранные группы пропускаются")

    print(f"[students] Групп: {len(groups_info)}")

    def skip_group(group_id, code):
        if not resume:
            return False
        total, with_subjects = group_subject_coverage(conn, group_id)
        return total > 0 and with_subjects >= total * RESUME_COVERAGE

    saved_groups = 0
    saved_students = 0
    updated_lessons = 0

    def on_group(group_id, students, teacher_updates):
        nonlocal saved_groups, saved_students, updated_lessons
        save_students(conn, group_id, students)

        for s in students:
            if not s.get('subjects'):
                continue
            row = conn.execute(
                "SELECT id FROM students WHERE group_id = ? AND site_id = ?",
                (group_id, s['site_id'])
            ).fetchone()
            if row:
                save_student_subjects(conn, row['id'], s['subjects'])

        updated_lessons += update_lesson_teachers(conn, teacher_updates)
        saved_groups += 1
        saved_students += len(students)
        print(f"    → сохранено в базу: {len(students)} студентов "
              f"(всего {saved_students}), преподавателей проставлено: {updated_lessons}")

    parser = SocioParser()

    try:
        parser.parse_students(groups_info, on_group=on_group, skip_group=skip_group)
    except Exception as e:
        message = f"{type(e).__name__}: {e} | {parser.stats_line()}"
        log_parse(conn, 'socio-students', 'partial', lessons_count=updated_lessons,
                  groups_count=saved_groups, message=message)
        conn.close()
        print(f"\n[students] Прогон оборван: {e}")
        print(f"[students] Сохранено до обрыва: {saved_groups} групп, {saved_students} студентов")
        print("[students] Продолжить с места обрыва: python run_parser.py students --resume")
        import traceback
        traceback.print_exc()
        alert_parse_error(
            'socio-students',
            f"Прогон оборван: {e}\n"
            f"Сохранено до обрыва: {saved_groups} групп / {saved_students} студентов\n"
            f"Продолжить: run_parser.py students --resume"
        )
        return

    status, message, problems = _compose(parser, [])

    log_parse(conn, 'socio-students', status, lessons_count=updated_lessons,
              groups_count=saved_groups, message=message)
    conn.close()

    print(f"\n[students] Сохранено: {saved_students} студентов из {saved_groups} групп, "
          f"преподавателей проставлено: {updated_lessons} занятий")
    _report('students', parser, problems)

    if problems:
        alert_parse_warning('socio-students', '\n'.join(problems))
    else:
        alert_parse_ok('socio-students', saved_students, updated_lessons)


# ======= Преподаватели через кафедры =======

def run_teachers():
    """Парсинг преподавателей через кафедры. Сохраняет после каждого преподавателя."""
    from parsers.socio import SocioParser

    conn = get_connection()
    groups = get_groups_for_student_parse(conn)

    if not groups:
        print("[teachers] Нет групп в базе.")
        conn.close()
        return

    group_code_to_id = {g['code']: g['id'] for g in groups}
    print(f"[teachers] Групп в маппинге: {len(group_code_to_id)}")

    updated = 0

    def on_teacher(updates):
        nonlocal updated
        updated += update_lesson_teachers(conn, updates)

    parser = SocioParser()

    try:
        result = parser.parse_teachers(group_code_to_id, on_teacher=on_teacher)
    except Exception as e:
        message = f"{type(e).__name__}: {e} | {parser.stats_line()}"
        log_parse(conn, 'socio-teachers', 'partial', lessons_count=updated, message=message)
        conn.close()
        print(f"\n[teachers] Прогон оборван: {e}")
        print(f"[teachers] Успели проставить: {updated} занятий")
        import traceback
        traceback.print_exc()
        alert_parse_error('socio-teachers',
                          f"Прогон оборван: {e}\nПроставлено до обрыва: {updated} занятий")
        return

    filled = fill_teachers_from_same_subject(conn)

    status, message, problems = _compose(parser, [])

    log_parse(conn, 'socio-teachers', status, lessons_count=updated,
              groups_count=result['teachers_found'], message=message)
    conn.close()

    print(f"\n[teachers] Преподавателей найдено: {result['teachers_found']}")
    print(f"[teachers] Обновлено: {updated} занятий")
    print(f"[teachers] Дозаполнено из лекций→семинаров: {filled}")
    _report('teachers', parser, problems)

    if problems:
        alert_parse_warning('socio-teachers', '\n'.join(problems))
    else:
        alert_parse_ok('socio-teachers', result['teachers_found'], updated)


# ======= Тест на локальном файле =======

def run_test():
    """Тест парсера на локальном HTML файле."""
    from parsers.socio import SocioParser

    filepath = None
    for a in sys.argv[1:]:
        if a != '--test' and not a.startswith('--'):
            filepath = a
            break
    filepath = filepath or 'socio.html'

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            html = f.read()
    except FileNotFoundError:
        print(f"Файл не найден: {filepath}")
        return

    parser = SocioParser()
    lessons = parser._parse_page(html)

    print(f"\nРезультат: {len(lessons)} занятий")
    if parser.unparsed_blocks:
        print(f"Нераспознано блоков: {parser.unparsed_blocks}")
        for sample in parser.unparsed_samples:
            print("   ", sample)
    print()

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
    commands = [a for a in args if not a.startswith('--')]

    if '--help' in args or '-h' in args:
        print(__doc__)
        return

    if '--test' in args:
        run_test()
    elif 'teachers' in commands:
        run_teachers()
    elif 'subgroups' in commands:
        run_subgroups()
    elif 'students' in commands:
        run_students()
    elif 'socio' in commands:
        run_socio()
    elif not args:
        run_socio()
        print("\n" + "=" * 60 + "\n")
        run_subgroups()          # после socio: сохраняет только то, чего нет у группы
        print("\n" + "=" * 60 + "\n")
        run_students()
        print("\n" + "=" * 60 + "\n")
        run_teachers()
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
