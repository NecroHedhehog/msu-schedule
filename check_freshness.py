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


from core.config import (PARSER_INTERVAL_HOURS, FRESHNESS_MAX_HOURS,
                         FRESHNESS_SKIP)

# Запасной порог для прохода, которого нет в FRESHNESS_MAX_HOURS:
# два интервала форы, прежде чем ругаться
DEFAULT_MAX_HOURS = PARSER_INTERVAL_HOURS * 2


def max_hours_for(code: str, override: float = None) -> float:
    """
    Порог свежести для конкретного прохода.

    Один общий порог не работает: socio ходит трижды в день, подгруппы —
    раз в сутки, преподаватели — трижды в неделю. С общим порогом в 8 часов
    каждая ночная проверка исправно сообщала, что суточный проход «устарел»
    через 15 часов. Это не находка, это его нормальный ритм.
    """
    if override is not None:
        return override
    return FRESHNESS_MAX_HOURS.get(code, DEFAULT_MAX_HOURS)


def check(max_hours: float = None):
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

        # Ручные проходы не следят за расписанием и «устаревают» всегда:
        # проход по студентам гоняют раз в семестр, и ночная тревога
        # о том, что он был двое суток назад, — ложная по построению.
        if code in FRESHNESS_SKIP:
            print(f"[freshness] {code}: пропущен, запускается руками")
            continue

        limit = max_hours_for(code, max_hours)

        last_ok = conn.execute(
            """SELECT created_at FROM parse_log
               WHERE faculty_code = ? AND status IN ('ok', 'warning')
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

        if hours > limit:
            alert_stale_data(code, hours_since=hours)
            print(f"[freshness] {code}: данные устарели ({hours:.0f}ч при пороге {limit:.0f}ч)")
        else:
            print(f"[freshness] {code}: ок ({hours:.1f}ч назад, порог {limit:.0f}ч)")

    conn.close()


if __name__ == '__main__':
    # None, а не DEFAULT_MAX_HOURS: иначе общий порог перебил бы
    # индивидуальные из FRESHNESS_MAX_HOURS, ради которых всё и затевалось.
    # --hours остаётся способом проверить вручную.
    max_h = None
    if '--hours' in sys.argv:
        idx = sys.argv.index('--hours')
        if idx + 1 < len(sys.argv):
            max_h = float(sys.argv[idx + 1])
    check(max_h)