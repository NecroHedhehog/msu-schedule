#!/usr/bin/env python3
"""
Убрать из базы занятия прошлого семестра.

Зачем. Коды групп на сайте переиспользуются между наборами: весенний «с101» —
это осенний «с201». get_or_create_group матчит группу по одному коду, поэтому
после первого же прогона нового семестра под кодом «с101» лежат и сентябрьские
занятия нынешних первокурсников, и мартовские занятия совсем других людей.
Бот при навигации по неделям назад покажет чужое расписание как своё.

По умолчанию НИЧЕГО НЕ УДАЛЯЕТ — показывает, что нашёл. Удаление только
по явному --yes, и всегда с резервной копией базы рядом.

Использование:
    python scripts/purge_old.py                          # сухой прогон
    python scripts/purge_old.py --before 2026-09-01      # сухой прогон с явной отсечкой
    python scripts/purge_old.py --before 2026-09-01 --yes
    python scripts/purge_old.py --before 2026-09-01 --students --yes
"""

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import DB_PATH

# Разрыв в датах больше стольких дней считаем границей семестра
SEMESTER_GAP_DAYS = 45


def connect() -> sqlite3.Connection:
    if not Path(DB_PATH).exists():
        sys.exit(f"Нет базы: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def detect_cutoff(conn) -> str | None:
    """Найти границу семестров по самому большому разрыву в датах."""
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM lessons ORDER BY date")]
    if len(dates) < 2:
        return None

    best_gap, best_date = 0, None
    prev = date.fromisoformat(dates[0])
    for raw in dates[1:]:
        cur = date.fromisoformat(raw)
        gap = (cur - prev).days
        if gap > best_gap:
            best_gap, best_date = gap, raw
        prev = cur

    if best_gap < SEMESTER_GAP_DAYS:
        return None
    print(f"  Найден разрыв в {best_gap} дней, следующий семестр начинается {best_date}")
    return best_date


def show_lessons(conn, cutoff: str):
    total = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    doomed = conn.execute(
        "SELECT COUNT(*) FROM lessons WHERE date < ?", (cutoff,)).fetchone()[0]

    print(f"\n  Занятий в базе: {total}")
    print(f"  Под удаление (раньше {cutoff}): {doomed}")
    print(f"  Останется: {total - doomed}")

    print("\n  По месяцам:")
    for r in conn.execute(
            "SELECT substr(date,1,7) AS m, COUNT(*) AS n FROM lessons "
            "GROUP BY m ORDER BY m"):
        mark = '  ← удаляем' if r['m'] < cutoff[:7] else ''
        print(f"    {r['m']}  {r['n']:5d}{mark}")

    affected = conn.execute(
        "SELECT COUNT(DISTINCT group_id) FROM lessons WHERE date < ?",
        (cutoff,)).fetchone()[0]
    print(f"\n  Затронуто групп: {affected}")

    print("\n  Группы, у которых занятия по обе стороны отсечки "
          "(там коды и переиспользованы):")
    rows = conn.execute(
        """SELECT g.code,
                  SUM(CASE WHEN l.date <  ? THEN 1 ELSE 0 END) AS old_n,
                  SUM(CASE WHEN l.date >= ? THEN 1 ELSE 0 END) AS new_n
             FROM lessons l JOIN groups_ g ON g.id = l.group_id
            GROUP BY g.code HAVING old_n > 0 AND new_n > 0
            ORDER BY old_n DESC LIMIT 10""", (cutoff, cutoff)).fetchall()
    for r in rows:
        print(f"    {r['code']:12s} старых {r['old_n']:4d} / новых {r['new_n']:4d}")
    if not rows:
        print("    (таких нет)")

    return doomed


def show_empty_groups(conn, cutoff: str):
    """
    Группы, у которых после чистки не останется ни одного занятия.
    Удалять их вслепую нельзя: часть просто ещё не получила расписание
    (у первокурсников это нормально), а часть — выпустившиеся наборы,
    которых на сайте уже нет. Различить их можно только по тому, встречалась
    ли группа в последнем обходе сайта, а этого база пока не помнит.
    """
    rows = conn.execute(
        """SELECT g.code, g.department, g.program,
                  (SELECT COUNT(*) FROM subscriptions s WHERE s.group_id = g.id) AS subs
             FROM groups_ g
            WHERE NOT EXISTS (SELECT 1 FROM lessons l
                               WHERE l.group_id = g.id AND l.date >= ?)
            ORDER BY subs DESC, g.code""", (cutoff,)).fetchall()

    if not rows:
        return

    print(f"\n  Групп без занятий после чистки: {len(rows)} (НЕ удаляются)")
    for r in rows:
        mark = f"  ← на неё подписаны: {r['subs']}" if r['subs'] else ''
        print(f"    {r['code']:12s} {r['department']}/{r['program']}{mark}")

    orphaned = sum(r['subs'] for r in rows)
    if orphaned:
        print(f"\n    Подписок на группы без расписания: {orphaned}.")
        print("    Эти люди в боте не увидят ничего. Часть таких групп —")
        print("    выпустившиеся наборы, которых на сайте уже нет.")


def show_students(conn):
    total = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    subjects = conn.execute("SELECT COUNT(*) FROM student_subjects").fetchone()[0]

    try:
        bound = conn.execute(
            "SELECT COUNT(*) FROM users WHERE student_id IS NOT NULL").fetchone()[0]
    except sqlite3.OperationalError:
        bound = 0

    print(f"\n  Студентов в базе: {total} (все из весеннего прогона)")
    print(f"  Записей о предметах студентов: {subjects}")
    print(f"  Пользователей бота с привязкой к студенту: {bound}")
    if bound:
        print("    ↑ эти привязки слетят, людям нужно будет привязаться заново")
    return total


def backup() -> Path:
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dst = Path(DB_PATH).with_name(f"schedule.backup-{stamp}.db")
    src = sqlite3.connect(str(DB_PATH))
    dst_conn = sqlite3.connect(str(dst))
    with dst_conn:
        src.backup(dst_conn)
    src.close()
    dst_conn.close()
    return dst


def main():
    ap = argparse.ArgumentParser(description='Убрать занятия прошлого семестра.')
    ap.add_argument('--before', default=None,
                    help='удалить занятия строго раньше этой даты (ГГГГ-ММ-ДД); '
                         'без флага граница ищется по разрыву в датах')
    ap.add_argument('--students', action='store_true',
                    help='заодно вычистить списки студентов и их предметы')
    ap.add_argument('--yes', action='store_true',
                    help='действительно удалить (без этого — только показать)')
    args = ap.parse_args()

    conn = connect()
    print(f"База: {DB_PATH}")

    cutoff = args.before
    if cutoff:
        try:
            date.fromisoformat(cutoff)
        except ValueError:
            sys.exit(f"Дата должна быть в формате ГГГГ-ММ-ДД, а не {cutoff!r}")
    else:
        cutoff = detect_cutoff(conn)
        if not cutoff:
            print("\n  Разрыва между семестрами не видно — база похожа на "
                  "один семестр. Если всё равно надо, задайте --before явно.")
            conn.close()
            return 0

    doomed = show_lessons(conn, cutoff)
    show_empty_groups(conn, cutoff)
    student_total = show_students(conn) if args.students else 0

    if not doomed and not student_total:
        print("\n  Удалять нечего.")
        conn.close()
        return 0

    if not args.yes:
        print("\n  Это сухой прогон, ничего не удалено.")
        print(f"  Чтобы удалить: python scripts/purge_old.py --before {cutoff}"
              f"{' --students' if args.students else ''} --yes")
        conn.close()
        return 0

    conn.close()
    path = backup()
    print(f"\n  Резервная копия: {path.name}")

    conn = connect()
    cur = conn.execute("DELETE FROM lessons WHERE date < ?", (cutoff,))
    removed_lessons = cur.rowcount

    removed_students = removed_subjects = 0
    if args.students:
        cur = conn.execute(
            "DELETE FROM student_subjects WHERE student_id IN (SELECT id FROM students)")
        removed_subjects = cur.rowcount
        cur = conn.execute("DELETE FROM students")
        removed_students = cur.rowcount
        try:
            conn.execute("UPDATE users SET student_id = NULL")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.execute("VACUUM")
    conn.close()

    print(f"\n  Удалено занятий: {removed_lessons}")
    if args.students:
        print(f"  Удалено студентов: {removed_students}, "
              f"записей о предметах: {removed_subjects}")
        print("  Привязки пользователей сброшены.")
    print("\n  Готово. Дальше: python run_parser.py students")
    return 0


if __name__ == '__main__':
    sys.exit(main())
