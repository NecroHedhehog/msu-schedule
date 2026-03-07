"""Работа с базой данных SQLite."""

import sqlite3
from pathlib import Path
from core.config import DB_PATH


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS faculties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            domain TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS groups_ (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            site_id TEXT,
            department TEXT,
            program TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (faculty_id) REFERENCES faculties(id),
            UNIQUE(faculty_id, code)
        );

        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            pair_number INTEGER NOT NULL,
            time_start TEXT NOT NULL,
            time_end TEXT NOT NULL,
            subject TEXT NOT NULL,
            subject_abbr TEXT,
            lesson_type TEXT,
            lesson_type_full TEXT,
            room TEXT,
            teacher TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups_(id)
        );

        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            notify_changes INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups_(id),
            UNIQUE(chat_id, group_id)
        );

        CREATE TABLE IF NOT EXISTS parse_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_code TEXT NOT NULL,
            status TEXT NOT NULL,
            lessons_count INTEGER DEFAULT 0,
            groups_count INTEGER DEFAULT 0,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, group_id, subject)
        );

        -- Пользователи (аналитика)
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            group_code TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Лог действий (аналитика)
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            detail TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_lessons_group_date ON lessons(group_id, date);
        CREATE INDEX IF NOT EXISTS idx_lessons_date ON lessons(date);
        CREATE INDEX IF NOT EXISTS idx_subscriptions_chat ON subscriptions(chat_id);
        CREATE INDEX IF NOT EXISTS idx_activity_log_chat ON activity_log(chat_id);
        CREATE INDEX IF NOT EXISTS idx_activity_log_ts ON activity_log(timestamp);
    """)
    conn.commit()


# === Парсер: факультеты и группы ===

def get_or_create_faculty(conn, code: str, name: str, domain: str) -> int:
    row = conn.execute("SELECT id FROM faculties WHERE code = ?", (code,)).fetchone()
    if row:
        return row['id']
    cursor = conn.execute(
        "INSERT INTO faculties (code, name, domain) VALUES (?, ?, ?)",
        (code, name, domain)
    )
    conn.commit()
    return cursor.lastrowid


def get_or_create_group(conn, faculty_id: int, code: str,
                         site_id: str = None, department: str = None,
                         program: str = None) -> int:
    row = conn.execute(
        "SELECT id FROM groups_ WHERE faculty_id = ? AND code = ?",
        (faculty_id, code)
    ).fetchone()
    if row:
        # обновить поля если они изменились
        conn.execute(
            """UPDATE groups_ SET site_id = ?, department = ?, program = ?
               WHERE id = ?""",
            (site_id, department, program, row['id'])
        )
        conn.commit()
        return row['id']
    cursor = conn.execute(
        "INSERT INTO groups_ (faculty_id, code, site_id, department, program) VALUES (?, ?, ?, ?, ?)",
        (faculty_id, code, site_id, department, program)
    )
    conn.commit()
    return cursor.lastrowid


def save_lessons(conn, group_id: int, lessons: list[dict]):
    if not lessons:
        return
    dates = set(l['date'] for l in lessons)
    placeholders = ','.join('?' for _ in dates)
    conn.execute(
        f"DELETE FROM lessons WHERE group_id = ? AND date IN ({placeholders})",
        [group_id] + list(dates)
    )
    conn.executemany(
        """INSERT INTO lessons
           (group_id, date, pair_number, time_start, time_end,
            subject, subject_abbr, lesson_type, lesson_type_full, room, teacher)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (group_id, l['date'], l['pair_number'], l['time_start'], l['time_end'],
             l['subject'], l.get('subject_abbr', ''), l.get('lesson_type', ''),
             l.get('lesson_type_full', ''), l.get('room', ''), l.get('teacher', ''))
            for l in lessons
        ]
    )
    conn.commit()


def log_parse(conn, faculty_code, status, lessons_count=0, groups_count=0, message=''):
    conn.execute(
        "INSERT INTO parse_log (faculty_code, status, lessons_count, groups_count, message) VALUES (?, ?, ?, ?, ?)",
        (faculty_code, status, lessons_count, groups_count, message)
    )
    conn.commit()


# === Бот: расписание ===

def get_lessons_for_date(conn, group_id: int, date: str) -> list:
    return conn.execute(
        "SELECT * FROM lessons WHERE group_id = ? AND date = ? ORDER BY pair_number, id",
        (group_id, date)
    ).fetchall()


def get_lessons_for_week(conn, group_id: int, start_date: str, end_date: str) -> list:
    return conn.execute(
        "SELECT * FROM lessons WHERE group_id = ? AND date BETWEEN ? AND ? ORDER BY date, pair_number, id",
        (group_id, start_date, end_date)
    ).fetchall()


def get_date_range(conn, group_id: int) -> tuple:
    """Диапазон дат, за которые есть данные. Возвращает (min_date, max_date) или (None, None)."""
    row = conn.execute(
        "SELECT MIN(date) as min_d, MAX(date) as max_d FROM lessons WHERE group_id = ?",
        (group_id,)
    ).fetchone()
    if row and row['min_d']:
        return row['min_d'], row['max_d']
    return None, None


# === Бот: пользователь и группа ===

def get_user_group(conn, chat_id: int) -> dict | None:
    row = conn.execute(
        """SELECT s.group_id, g.code as group_code, g.department, g.program,
                  f.name as faculty_name, f.code as faculty_code
           FROM subscriptions s
           JOIN groups_ g ON s.group_id = g.id
           JOIN faculties f ON g.faculty_id = f.id
           WHERE s.chat_id = ? LIMIT 1""",
        (chat_id,)
    ).fetchone()
    return dict(row) if row else None


def set_user_group(conn, chat_id: int, group_id: int):
    conn.execute("DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,))
    conn.execute(
        "INSERT INTO subscriptions (chat_id, group_id) VALUES (?, ?)",
        (chat_id, group_id)
    )
    conn.commit()


# === Бот: предметы по выбору ===

def get_conflicting_subjects(conn, group_id: int) -> list[dict]:
    """Предметы, которые стоят на одну пару (предметы по выбору)."""
    rows = conn.execute(
        """SELECT date, pair_number, time_start, subject, subject_abbr, room, lesson_type
           FROM lessons WHERE group_id = ? AND date >= date('now')
           ORDER BY date, pair_number, subject""",
        (group_id,)
    ).fetchall()

    from collections import defaultdict
    slots = defaultdict(list)
    for r in rows:
        slots[(r['date'], r['pair_number'])].append(dict(r))

    seen = set()
    result = []
    for lessons in slots.values():
        if len(lessons) > 1:
            for l in lessons:
                if l['subject'] not in seen:
                    seen.add(l['subject'])
                    result.append(l)
    return result


def get_user_subjects(conn, chat_id: int, group_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT subject FROM user_subjects WHERE chat_id = ? AND group_id = ?",
        (chat_id, group_id)
    ).fetchall()
    return [r['subject'] for r in rows]


def toggle_user_subject(conn, chat_id: int, group_id: int, subject: str) -> bool:
    existing = conn.execute(
        "SELECT id FROM user_subjects WHERE chat_id = ? AND group_id = ? AND subject = ?",
        (chat_id, group_id, subject)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM user_subjects WHERE id = ?", (existing['id'],))
        conn.commit()
        return False
    else:
        conn.execute(
            "INSERT INTO user_subjects (chat_id, group_id, subject) VALUES (?, ?, ?)",
            (chat_id, group_id, subject)
        )
        conn.commit()
        return True


# === Аналитика ===

def track_user(conn, chat_id: int, username: str = None,
               first_name: str = None, last_name: str = None,
               group_code: str = None):
    """Сохранить/обновить данные пользователя."""
    existing = conn.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    if existing:
        updates = ["last_active = CURRENT_TIMESTAMP"]
        params = []
        if username is not None:
            updates.append("username = ?")
            params.append(username)
        if first_name is not None:
            updates.append("first_name = ?")
            params.append(first_name)
        if last_name is not None:
            updates.append("last_name = ?")
            params.append(last_name)
        if group_code is not None:
            updates.append("group_code = ?")
            params.append(group_code)
        params.append(chat_id)
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE chat_id = ?", params)
    else:
        conn.execute(
            "INSERT INTO users (chat_id, username, first_name, last_name, group_code) VALUES (?, ?, ?, ?, ?)",
            (chat_id, username, first_name, last_name, group_code)
        )
    conn.commit()


def log_action(conn, chat_id: int, action: str, detail: str = None):
    """Записать действие пользователя."""
    conn.execute(
        "INSERT INTO activity_log (chat_id, action, detail) VALUES (?, ?, ?)",
        (chat_id, action, detail)
    )
    conn.commit()


def get_stats(conn) -> dict:
    """Статистика для /stats."""
    total = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()['c']
    active_7d = conn.execute(
        "SELECT COUNT(*) as c FROM users WHERE last_active > datetime('now', '-7 days')"
    ).fetchone()['c']
    today_actions = conn.execute(
        "SELECT COUNT(*) as c FROM activity_log WHERE timestamp > date('now')"
    ).fetchone()['c']
    ad_clicks = conn.execute(
        "SELECT COUNT(*) as c FROM activity_log WHERE action = 'ad_click'"
    ).fetchone()['c']
    top_groups = conn.execute(
        """SELECT group_code, COUNT(*) as cnt FROM users
           WHERE group_code IS NOT NULL AND group_code != ''
           GROUP BY group_code ORDER BY cnt DESC LIMIT 10"""
    ).fetchall()
    return {
        'total': total,
        'active_7d': active_7d,
        'today_actions': today_actions,
        'ad_clicks': ad_clicks,
        'top_groups': [(r['group_code'], r['cnt']) for r in top_groups],
    }
