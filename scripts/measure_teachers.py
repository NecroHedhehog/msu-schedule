#!/usr/bin/env python3
"""
Замер: сколько преподавателей даёт кафедральный проход САМ ПО СЕБЕ.

Зачем. В README вклад трёх этапов записан как 77% + 2% + 3%. Но это
предельные прибавки в том порядке, в котором их запускали: update_lesson_teachers
пишет только туда, где преподаватель пуст, а кафедральный проход стартовал
по базе, где 77% уже было занято студенческим. Ему физически осталось 23%,
из которых почти всё — МФК и физкультура, у которых преподавателя нет на
самом сайте. Сколько он даёт с нуля — никто не мерил.

Если окажется 70-80%, студенческий проход (3303 запроса, ~35 минут) можно
убрать из пакетного цикла: кафедральный отдаёт тот же кортеж
(дата, пара, предмет, коды групп) -> преподаватель за 586 запросов.

Работает на КОПИИ базы, оригинал не трогает. Алерты в Telegram отключены.

Использование:
    python scripts/measure_teachers.py --report-only   # только текущий расклад, без сети
    python scripts/measure_teachers.py                 # полный замер (~586 запросов, ~10 мин)
"""

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC_DB = ROOT / 'data' / 'schedule.db'
COPY_DB = ROOT / 'data' / 'measure_teachers.db'

# Предметы, у которых преподавателя нет на самом сайте — их нельзя
# требовать от парсера, и в потолок покрытия они не входят
NO_TEACHER_PATTERNS = ('%Межфакультетские%', '%физической культуре%')


def open_copy() -> sqlite3.Connection:
    conn = sqlite3.connect(str(COPY_DB))
    conn.row_factory = sqlite3.Row
    return conn


def make_copy():
    """Честная копия через backup API: файловое cp теряет содержимое WAL."""
    if not SRC_DB.exists():
        sys.exit(f"Нет базы: {SRC_DB}")
    if COPY_DB.exists():
        COPY_DB.unlink()
    src = sqlite3.connect(str(SRC_DB))
    dst = sqlite3.connect(str(COPY_DB))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"Копия базы: {COPY_DB.name}")


def coverage(conn) -> dict:
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]

    total = q("SELECT COUNT(*) FROM lessons")
    filled = q("SELECT COUNT(*) FROM lessons WHERE teacher IS NOT NULL AND teacher != ''")

    no_teacher_clause = ' OR '.join("subject LIKE ?" for _ in NO_TEACHER_PATTERNS)
    unreachable = conn.execute(
        f"SELECT COUNT(*) FROM lessons WHERE {no_teacher_clause}", NO_TEACHER_PATTERNS
    ).fetchone()[0]

    achievable = total - unreachable
    return {
        'total': total,
        'filled': filled,
        'unreachable': unreachable,
        'achievable': achievable,
        'pct_of_total': filled / total * 100 if total else 0,
        'pct_of_achievable': filled / achievable * 100 if achievable else 0,
    }


def show(label: str, c: dict):
    print(f"\n  {label}")
    print(f"    занятий всего:            {c['total']}")
    print(f"    с преподавателем:         {c['filled']}  "
          f"({c['pct_of_total']:.1f}% от всех)")
    print(f"    без препода на сайте:     {c['unreachable']}  (МФК и физкультура)")
    print(f"    достижимый потолок:       {c['achievable']}  "
          f"→ покрытие {c['pct_of_achievable']:.1f}%")


def report_only():
    print("=" * 64)
    print("Текущий расклад в базе (ничего не меняется, сеть не нужна)")
    print("=" * 64)
    conn = open_copy() if COPY_DB.exists() else sqlite3.connect(str(SRC_DB))
    conn.row_factory = sqlite3.Row
    show('как есть сейчас', coverage(conn))

    print("\n  Занятия без преподавателя — по предметам:")
    rows = conn.execute(
        """SELECT subject, COUNT(*) AS c FROM lessons
           WHERE teacher IS NULL OR teacher = ''
           GROUP BY subject ORDER BY c DESC LIMIT 12"""
    ).fetchall()
    for r in rows:
        print(f"    {r['c']:5d}  {r['subject'][:60]}")
    conn.close()
    print()


def measure():
    make_copy()

    # Считаем на копии, а не на боевой базе
    os.environ['DB_PATH'] = str(COPY_DB.relative_to(ROOT)).replace('\\', '/')

    import core.alerts as alerts
    alerts.send_admin_alert = lambda text: print(f"  [alert подавлен] {text.splitlines()[0]}")

    from core.database import get_connection
    from core.db_students import (
        get_groups_for_student_parse, update_lesson_teachers,
        fill_teachers_from_same_subject,
    )
    from parsers.socio import SocioParser

    conn = get_connection()
    before = coverage(conn)
    show('ДО замера (как собрано тремя проходами)', before)

    print("\n  Обнуляю преподавателей на копии...")
    conn.execute("UPDATE lessons SET teacher = ''")
    conn.commit()
    show('после обнуления', coverage(conn))

    groups = get_groups_for_student_parse(conn)
    group_code_to_id = {g['code']: g['id'] for g in groups}

    updated = 0

    def on_teacher(updates):
        nonlocal updated
        updated += update_lesson_teachers(conn, updates)

    print(f"\n  Запускаю ТОЛЬКО кафедральный проход ({len(group_code_to_id)} групп в маппинге)...")
    parser = SocioParser()
    try:
        result = parser.parse_teachers(group_code_to_id, on_teacher=on_teacher)
    except Exception as e:
        print(f"\n  Прогон оборван: {e}")
        print(f"  Успели проставить: {updated} занятий")
        show('на момент обрыва', coverage(conn))
        print(f"  {parser.stats_line()}")
        conn.close()
        return

    after_chairs = coverage(conn)
    show('ПОСЛЕ кафедрального прохода (сам по себе, с нуля)', after_chairs)

    filled = fill_teachers_from_same_subject(conn)
    after_fill = coverage(conn)
    show(f'+ дозаполнение лекция→семинар ({filled} занятий)', after_fill)

    conn.close()

    print("\n" + "=" * 64)
    print("ИТОГ")
    print("=" * 64)
    print(f"  преподавателей на кафедрах:      {result['teachers_found']}")
    print(f"  {parser.stats_line()}")
    print(f"  было тремя проходами:            {before['pct_of_achievable']:.1f}% от достижимого")
    print(f"  один кафедральный проход:        {after_chairs['pct_of_achievable']:.1f}%")
    print(f"  он же + дозаполнение:            {after_fill['pct_of_achievable']:.1f}%")
    print()
    delta = before['pct_of_achievable'] - after_fill['pct_of_achievable']
    if delta <= 5:
        print(f"  Разница {delta:.1f} п.п. — студенческий проход почти ничего не добавляет.")
        print("  Значит 3303 запроса из пакетного цикла можно убрать, а предметы")
        print("  студента догружать по требованию при привязке (3 запроса на человека).")
    else:
        print(f"  Разница {delta:.1f} п.п. — студенческий проход даёт заметный вклад.")
        print("  Убирать его из цикла нельзя, но можно гонять реже кафедрального.")
    print(f"\n  Копия базы осталась здесь: {COPY_DB}")
    print("  Боевая data/schedule.db не тронута.\n")


if __name__ == '__main__':
    if '--report-only' in sys.argv:
        report_only()
    else:
        measure()
