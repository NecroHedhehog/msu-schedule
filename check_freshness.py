#!/usr/bin/env python3
"""
Проверка актуальности данных.

Смотрит в parse_log: если последний успешный парсинг был давно — шлёт алерт.
Запускать по крону раз в 6-12 часов, отдельно от парсера.

Использование:
    python check_freshness.py
    python check_freshness.py --hours 12
"""

import sys
from core.database import get_connection
from core.alerts import alert_stale_data


DEFAULT_MAX_HOURS = 8


def check(max_hours: float = DEFAULT_MAX_HOURS):
    conn = get_connection()

    faculties = conn.execute(
        "SELECT DISTINCT faculty_code FROM parse_log"
    ).fetchall()

    if not faculties:
        print("[freshness] Нет записей в parse_log — парсер ни разу не запускался.")
        conn.close()
        return

    for row in faculties:
        code = row['faculty_code']

        last_ok = conn.execute(
            """SELECT created_at FROM parse_log
               WHERE faculty_code = ? AND status = 'ok'
               ORDER BY created_at DESC LIMIT 1""",
            (code,)
        ).fetchone()

        if not last_ok:
            alert_stale_data(code, hours_since=999)
            print(f"[freshness] {code}: нет успешных парсингов!")
            continue

        hours = conn.execute(
            "SELECT (julianday('now') - julianday(?)) * 24 as hours",
            (last_ok['created_at'],)
        ).fetchone()['hours']

        if hours > max_hours:
            alert_stale_data(code, hours_since=hours)
            print(f"[freshness] {code}: данные устарели ({hours:.0f}ч)")
        else:
            print(f"[freshness] {code}: ок ({hours:.1f}ч назад)")

    conn.close()


if __name__ == '__main__':
    max_h = DEFAULT_MAX_HOURS
    if '--hours' in sys.argv:
        idx = sys.argv.index('--hours')
        if idx + 1 < len(sys.argv):
            max_h = float(sys.argv[idx + 1])
    check(max_h)