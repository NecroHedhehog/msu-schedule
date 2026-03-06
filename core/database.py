"""
Работа с базой данных SQLite.
Единый источник данных для бота и сайта.
"""

import sqlite3
from pathlib import Path
from core.config import DB_PATH


def get_connection() -> sqlite3.Connection:
    """Получить соединение с базой. Создаёт файл и таблицы если их нет."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row  # доступ к колонкам по имени
    conn.execute("PRAGMA journal_mode=WAL")  # для параллельного чтения
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection):
    """Создание таблиц при первом запуске."""
    conn.executescript("""
        -- Факультеты
        CREATE TABLE IF NOT EXISTS faculties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,      -- 'socio', 'spa', 'law'
            name TEXT NOT NULL,             -- 'Социологический факультет'
            domain TEXT NOT NULL,           -- 'cacs.socio.msu.ru'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Группы
        CREATE TABLE IF NOT EXISTS groups_ (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_id INTEGER NOT NULL,
            code TEXT NOT NULL,             -- 'с403'
            site_id TEXT,                   -- '279' (параметр ?gr= на сайте)
            department TEXT,                -- 'Бакалавриат'
            program TEXT,                   -- 'Социология'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (faculty_id) REFERENCES faculties(id),
            UNIQUE(faculty_id, code)
        );

        -- Занятия (расписание)
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            date TEXT NOT NULL,             -- '2026-03-05' (ISO формат)
            pair_number INTEGER NOT NULL,   -- 1-6
            time_start TEXT NOT NULL,       -- '12:55'
            time_end TEXT NOT NULL,         -- '14:25'
            subject TEXT NOT NULL,          -- полное название
            subject_abbr TEXT,             -- 'КАД'
            lesson_type TEXT,              -- 'Лк', 'Сем', 'Зч', 'Экз'
            lesson_type_full TEXT,         -- 'Лекция', 'Семинар'
            room TEXT,                     -- '417'
            teacher TEXT,                  -- 'Иванов А.Б.'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups_(id)
        );

        -- Подписки пользователей (для бота)
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,       -- Telegram chat ID
            group_id INTEGER NOT NULL,
            notify_changes INTEGER DEFAULT 1,  -- уведомлять об изменениях?
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups_(id),
            UNIQUE(chat_id, group_id)
        );

        -- Лог парсинга (для мониторинга)
        CREATE TABLE IF NOT EXISTS parse_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_code TEXT NOT NULL,
            status TEXT NOT NULL,           -- 'ok', 'error'
            lessons_count INTEGER DEFAULT 0,
            groups_count INTEGER DEFAULT 0,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- Выбранные предметы пользователя (фильтр предметов по выбору)
        CREATE TABLE IF NOT EXISTS user_subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            subject TEXT NOT NULL,           -- полное название предмета
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, group_id, subject)
        );

        -- Индексы для быстрых запросов
        CREATE INDEX IF NOT EXISTS idx_lessons_group_date
            ON lessons(group_id, date);
        CREATE INDEX IF NOT EXISTS idx_lessons_date
            ON lessons(date);
        CREATE INDEX IF NOT EXISTS idx_subscriptions_chat
            ON subscriptions(chat_id);
    """)
    conn.commit()


# === Удобные функции для работы с данными ===

def get_or_create_faculty(conn, code: str, name: str, domain: str) -> int:
    """Получить или создать факультет. Возвращает ID."""
    row = conn.execute(
        "SELECT id FROM faculties WHERE code = ?", (code,)
    ).fetchone()
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
    """Получить или создать группу. Возвращает ID."""
    row = conn.execute(
        "SELECT id FROM groups_ WHERE faculty_id = ? AND code = ?",
        (faculty_id, code)
    ).fetchone()
    if row:
        return row['id']
    cursor = conn.execute(
        """INSERT INTO groups_ (faculty_id, code, site_id, department, program)
           VALUES (?, ?, ?, ?, ?)""",
        (faculty_id, code, site_id, department, program)
    )
    conn.commit()
    return cursor.lastrowid


def save_lessons(conn, group_id: int, lessons: list[dict]):
    """
    Сохранить занятия для группы.
    Удаляет старые данные для дат, которые есть в новых данных,
    и вставляет новые.
    """
    if not lessons:
        return

    # Какие даты обновляем
    dates = set(lesson['date'] for lesson in lessons)
    placeholders = ','.join('?' for _ in dates)

    # Удаляем старые записи только за эти даты
    conn.execute(
        f"DELETE FROM lessons WHERE group_id = ? AND date IN ({placeholders})",
        [group_id] + list(dates)
    )

    # Вставляем новые
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


def get_lessons_for_date(conn, group_id: int, date: str) -> list:
    """Получить расписание группы на дату (формат YYYY-MM-DD)."""
    return conn.execute(
        """SELECT * FROM lessons
           WHERE group_id = ? AND date = ?
           ORDER BY pair_number, id""",
        (group_id, date)
    ).fetchall()


def get_lessons_for_week(conn, group_id: int, start_date: str, end_date: str) -> list:
    """Получить расписание группы на период."""
    return conn.execute(
        """SELECT * FROM lessons
           WHERE group_id = ? AND date BETWEEN ? AND ?
           ORDER BY date, pair_number, id""",
        (group_id, start_date, end_date)
    ).fetchall()


def log_parse(conn, faculty_code: str, status: str,
              lessons_count: int = 0, groups_count: int = 0, message: str = ''):
    """Записать лог парсинга."""
    conn.execute(
        """INSERT INTO parse_log (faculty_code, status, lessons_count, groups_count, message)
           VALUES (?, ?, ?, ?, ?)""",
        (faculty_code, status, lessons_count, groups_count, message)
    )
    conn.commit()


# === Функции для бота ===

def get_user_group(conn, chat_id: int) -> dict | None:
    """Получить группу пользователя."""
    row = conn.execute(
        """SELECT s.group_id, g.code as group_code, g.department, g.program,
                  f.name as faculty_name, f.code as faculty_code
           FROM subscriptions s
           JOIN groups_ g ON s.group_id = g.id
           JOIN faculties f ON g.faculty_id = f.id
           WHERE s.chat_id = ?
           LIMIT 1""",
        (chat_id,)
    ).fetchone()
    return dict(row) if row else None


def set_user_group(conn, chat_id: int, group_id: int):
    """Установить группу пользователя."""
    conn.execute(
        "DELETE FROM subscriptions WHERE chat_id = ?", (chat_id,)
    )
    conn.execute(
        "INSERT INTO subscriptions (chat_id, group_id) VALUES (?, ?)",
        (chat_id, group_id)
    )
    conn.commit()


def get_all_faculties(conn) -> list:
    """Список всех факультетов."""
    return conn.execute("SELECT * FROM faculties ORDER BY name").fetchall()


def get_departments(conn, faculty_id: int) -> list:
    """Список отделений (уникальные department) факультета."""
    return conn.execute(
        """SELECT DISTINCT department FROM groups_
           WHERE faculty_id = ? AND department != ''
           ORDER BY department""",
        (faculty_id,)
    ).fetchall()


def get_groups_by_department(conn, faculty_id: int, department: str) -> list:
    """Список групп отделения."""
    return conn.execute(
        """SELECT * FROM groups_
           WHERE faculty_id = ? AND department = ?
           ORDER BY code""",
        (faculty_id, department)
    ).fetchall()


def get_all_groups(conn, faculty_id: int) -> list:
    """Список всех групп факультета."""
    return conn.execute(
        "SELECT * FROM groups_ WHERE faculty_id = ? ORDER BY code",
        (faculty_id,)
    ).fetchall()


def get_conflicting_subjects(conn, group_id: int) -> list[dict]:
    """
    Найти пары, где на одно время стоит больше одного предмета.
    Это предметы по выбору — студенту нужно выбрать.
    """
    rows = conn.execute(
        """SELECT date, pair_number, time_start, subject, subject_abbr, room, lesson_type
           FROM lessons
           WHERE group_id = ?
           AND date >= date('now')
           ORDER BY date, pair_number, subject""",
        (group_id,)
    ).fetchall()

    # Группируем по (date, pair_number)
    from collections import defaultdict
    slots = defaultdict(list)
    for r in rows:
        key = (r['date'], r['pair_number'])
        slots[key].append(dict(r))

    # Находим слоты с >1 предметом
    conflicts = []
    seen_subjects = set()
    for key, lessons in slots.items():
        if len(lessons) > 1:
            for l in lessons:
                if l['subject'] not in seen_subjects:
                    seen_subjects.add(l['subject'])
                    conflicts.append(l)

    # Возвращаем уникальные предметы
    unique = []
    seen = set()
    for c in conflicts:
        if c['subject'] not in seen:
            seen.add(c['subject'])
            unique.append(c)
    return unique


def get_user_subjects(conn, chat_id: int, group_id: int) -> list[str]:
    """Получить выбранные предметы пользователя."""
    rows = conn.execute(
        "SELECT subject FROM user_subjects WHERE chat_id = ? AND group_id = ?",
        (chat_id, group_id)
    ).fetchall()
    return [r['subject'] for r in rows]


def toggle_user_subject(conn, chat_id: int, group_id: int, subject: str) -> bool:
    """
    Переключить предмет: если выбран — убрать, если нет — добавить.
    Возвращает True если предмет теперь выбран.
    """
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


def get_filtered_lessons(conn, group_id: int, date: str, chat_id: int = None) -> list:
    """
    Получить расписание на дату с учётом выбранных предметов.
    Если у пользователя есть выбранные предметы — фильтрует.
    Если нет — показывает всё.
    """
    lessons = get_lessons_for_date(conn, group_id, date)

    if not chat_id:
        return lessons

    # Получить выбранные предметы
    user_subjects = get_user_subjects(conn, chat_id, group_id)
    if not user_subjects:
        return lessons  # нет фильтра — показываем всё

    # Группируем по паре
    from collections import defaultdict
    by_pair = defaultdict(list)
    for l in lessons:
        by_pair[l['pair_number']].append(l)

    filtered = []
    for pair_num in sorted(by_pair.keys()):
        pair_lessons = by_pair[pair_num]
        if len(pair_lessons) == 1:
            # Единственный предмет — показываем всегда
            filtered.extend(pair_lessons)
        else:
            # Несколько предметов — показываем только выбранные
            for l in pair_lessons:
                if l['subject'] in user_subjects:
                    filtered.append(l)
            # Если ничего не выбрано из этого слота — показываем все
            if not any(l['subject'] in user_subjects for l in pair_lessons):
                filtered.extend(pair_lessons)

    return filtered
