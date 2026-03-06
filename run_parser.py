#!/usr/bin/env python3
"""
Точка запуска парсера расписания.

Использование:
    python run_parser.py              — парсить все факультеты
    python run_parser.py socio        — только соцфак
    python run_parser.py --test       — тест на локальном HTML файле
"""

import sys
from core.database import (
    get_connection, get_or_create_faculty, get_or_create_group,
    save_lessons, log_parse
)


def run_socio():
    """Запустить парсер соцфака и сохранить в базу."""
    from parsers.socio import SocioParser

    parser = SocioParser()
    result = parser.parse()

    if not result['groups']:
        print("❌ Парсер не вернул данных!")
        conn = get_connection()
        log_parse(conn, 'socio', 'error', message='Нет данных')
        conn.close()
        return

    # Сохраняем в базу
    conn = get_connection()
    faculty_id = get_or_create_faculty(
        conn,
        code=parser.FACULTY_CODE,
        name=parser.FACULTY_NAME,
        domain=parser.DOMAIN,
    )

    total_lessons = 0
    for group_data in result['groups']:
        group_id = get_or_create_group(
            conn,
            faculty_id=faculty_id,
            code=group_data['code'],
            site_id=group_data.get('site_id', ''),
            department=group_data.get('department', ''),
            program=group_data.get('program', ''),
        )
        save_lessons(conn, group_id, group_data['lessons'])
        total_lessons += len(group_data['lessons'])

    log_parse(
        conn, 'socio', 'ok',
        lessons_count=total_lessons,
        groups_count=len(result['groups']),
    )
    conn.close()

    print(f"💾 Сохранено в базу: {len(result['groups'])} групп, {total_lessons} занятий")


def run_test():
    """
    Тест парсера на локальном HTML файле.
    Использование: python run_parser.py --test socio.html
    """
    from parsers.socio import SocioParser

    filepath = sys.argv[2] if len(sys.argv) > 2 else 'socio.html'

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            html = f.read()
    except FileNotFoundError:
        print(f"❌ Файл не найден: {filepath}")
        print(f"   Скачай HTML расписания: Ctrl+U на странице → сохрани в файл")
        return

    parser = SocioParser()
    lessons = parser._parse_schedule_page(html)

    print(f"\n📊 Результат: {len(lessons)} занятий\n")

    # Группируем по дате
    from collections import defaultdict
    by_date = defaultdict(list)
    for l in lessons:
        by_date[l['date']].append(l)

    for dt in sorted(by_date.keys()):
        entries = sorted(by_date[dt], key=lambda x: (x['pair_number'], x['subject_abbr']))
        print(f"📅 {dt}")
        for e in entries:
            print(f"   {e['pair_number']} пара ({e['time_start']}-{e['time_end']}) "
                  f"| {e['subject_abbr']:10s} | ауд.{e['room']:4s} "
                  f"[{e['lesson_type']:3s}] — {e['subject']}")
        print()


def main():
    args = sys.argv[1:]

    if '--test' in args:
        run_test()
    elif not args or 'socio' in args:
        run_socio()
    # Будущие парсеры:
    # elif 'spa' in args:
    #     run_spa()
    else:
        print("Использование:")
        print("  python run_parser.py          — парсить все факультеты")
        print("  python run_parser.py socio    — только соцфак")
        print("  python run_parser.py --test   — тест на локальном файле")
        print("  python run_parser.py --test socio.html")


if __name__ == '__main__':
    main()
