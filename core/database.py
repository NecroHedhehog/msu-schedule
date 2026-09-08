"""Работа с базой данных SQLite."""

import sqlite3
from pathlib import Path
from core.config import DB_PATH, SHRINK_GUARD_RATIO


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _register_functions(conn)
    _create_tables(conn)
    _migrate(conn)
    return conn


# Колонки, добавленные после первого выпуска. Для новых баз они уже есть
# в _create_tables, для существующих добавляются здесь.
_ADDED_COLUMNS = (
    ('lessons', 'subgroup', "TEXT DEFAULT ''"),
    ('users', 'student_id', 'INTEGER'),
)


def _migrate(conn: sqlite3.Connection):
    """
    Дописать недостающие колонки в уже существующую базу.

    Раньше это делалось ALTER'ом в try/except прямо посреди работы бота
    (в bind_student), то есть при каждой привязке студента. Теперь один раз
    при открытии соединения и по явному списку.
    """
    for table, column, decl in _ADDED_COLUMNS:
        existing = {r['name'] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue          # таблицы ещё нет — её создаст _create_tables
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            conn.commit()


def _register_functions(conn: sqlite3.Connection):
    """
    Встроенный LOWER() в SQLite работает только с ASCII: lower('ИВАНОВА')
    возвращает 'ИВАНОВА'. Из-за этого LIKE по кириллице оказывался
    регистрозависимым, и поиск по фамилии с маленькой буквы не находил никого.
    Питоновский str.lower() кириллицу понимает.
    """
    conn.create_function('pylower', 1, lambda v: v.lower() if v else v)


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
            -- Пустая строка — обычное занятие всей группы.
            -- Непустая — поток подгруппы (языки): «с101-3».
            subgroup TEXT DEFAULT '',
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

        -- Выбранный человеком языковой поток.
        -- Хранится СМЫСЛОМ (предмет + преподаватель), а не номером потока:
        -- номер «с101-3» живёт один семестр, потому что коды групп
        -- пересобираются, а студент переходит на следующий курс.
        CREATE TABLE IF NOT EXISTS user_streams (
            chat_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            teacher TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chat_id, group_id)
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


def count_lessons(conn, group_id: int) -> int:
    """Сколько занятий уже лежит у группы."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM lessons WHERE group_id = ?", (group_id,)
    ).fetchone()['c']


def save_lessons(conn, group_id: int, lessons: list[dict], shrink_guard: bool = True) -> dict:
    """
    Записать занятия группы за пришедшие даты.

    shrink_guard: если сайт икнул и отдал огрызок (сильно меньше того, что уже
    лежит в базе за те же даты) — ничего не трогаем и говорим об этом наверх.
    Иначе один плохой ответ стирает нормальное расписание.

    Возвращает {'written': int, 'skipped': bool, 'reason': str}.
    """
    if not lessons:
        return {'written': 0, 'skipped': False, 'reason': 'нет занятий'}

    dates = sorted(set(l['date'] for l in lessons))
    placeholders = ','.join('?' for _ in dates)

    existing = conn.execute(
        f"""SELECT COUNT(*) AS c FROM lessons
             WHERE group_id = ? AND subgroup = '' AND date IN ({placeholders})""",
        [group_id] + dates
    ).fetchone()['c']

    if shrink_guard and existing and len(lessons) < existing * SHRINK_GUARD_RATIO:
        return {
            'written': 0,
            'skipped': True,
            'reason': f"пришло {len(lessons)} занятий против {existing} в базе за те же даты",
        }

    # Только занятия самой группы: расписание подгрупп собирается
    # отдельным проходом и стирать его тут нельзя
    conn.execute(
        f"""DELETE FROM lessons
             WHERE group_id = ? AND subgroup = '' AND date IN ({placeholders})""",
        [group_id] + dates
    )
    _insert_lessons(conn, group_id, lessons, subgroup='')
    conn.commit()
    return {'written': len(lessons), 'skipped': False, 'reason': ''}


def _insert_lessons(conn, group_id: int, lessons: list, subgroup: str):
    conn.executemany(
        """INSERT INTO lessons
           (group_id, date, pair_number, time_start, time_end,
            subject, subject_abbr, lesson_type, lesson_type_full, room,
            teacher, subgroup)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (group_id, l['date'], l['pair_number'], l['time_start'], l['time_end'],
             l['subject'], l.get('subject_abbr', ''), l.get('lesson_type', ''),
             l.get('lesson_type_full', ''), l.get('room', ''), l.get('teacher', ''),
             subgroup)
            for l in lessons
        ]
    )


def save_subgroup_lessons(conn, group_id: int, subgroup: str,
                          lessons: list, only_new: bool = True) -> int:
    """
    Записать занятия одного потока подгруппы (языки).

    Отдельно от save_lessons и намеренно: занятия группы и занятия её
    подгрупп живут в одной таблице, но собираются разными проходами.
    Если бы удаление шло по одним только датам, второй проход стирал бы
    результат первого. Поэтому чистим строго свою подгруппу.

    only_new: страница подгруппы показывает не только её язык, но и всё
    обычное расписание группы. Записывать это целиком — значит показать
    человеку каждую пару дважды. Поэтому по умолчанию оставляем только то,
    чего у группы нет. Требует, чтобы расписание группы уже лежало в базе:
    проход по подгруппам идёт ПОСЛЕ socio.
    """
    if not subgroup:
        raise ValueError("save_subgroup_lessons: нужна непустая метка подгруппы")
    if not lessons:
        return 0

    if only_new:
        own = {
            (r['date'], r['pair_number'], r['subject'])
            for r in conn.execute(
                "SELECT date, pair_number, subject FROM lessons "
                "WHERE group_id = ? AND subgroup = ''", (group_id,))
        }
        lessons = [l for l in lessons
                   if (l['date'], l['pair_number'], l['subject']) not in own]
        if not lessons:
            return 0

    dates = sorted(set(l['date'] for l in lessons))
    placeholders = ','.join('?' for _ in dates)
    conn.execute(
        f"""DELETE FROM lessons
             WHERE group_id = ? AND subgroup = ? AND date IN ({placeholders})""",
        [group_id, subgroup] + dates
    )
    _insert_lessons(conn, group_id, lessons, subgroup=subgroup)
    conn.commit()
    return len(lessons)


def delete_streams_by_subject(conn, markers) -> int:
    """
    Убрать из базы потоки, которые учат предмет из списка маркеров.

    Удаляется поток ЦЕЛИКОМ, а не только совпавшие занятия: у потока для
    иностранных студентов кроме русского языка бывает своя программа —
    педагогика, философия, методология, — и она такой же чужой материал.

    Нужно, чтобы список маркеров можно было менять и база подчищалась сама,
    без ручного SQL.
    """
    markers = [m for m in markers if m]
    if not markers:
        return 0

    like = ' OR '.join("x.subject LIKE ?" for _ in markers)
    cur = conn.execute(
        f"""DELETE FROM lessons
             WHERE subgroup != ''
               AND EXISTS (SELECT 1 FROM lessons x
                            WHERE x.group_id = lessons.group_id
                              AND x.subgroup = lessons.subgroup
                              AND ({like}))""",
        [f'%{m}%' for m in markers]
    )
    conn.commit()
    return cur.rowcount


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


def find_groups_by_code(conn, query: str, limit: int = 20) -> list:
    """
    Поиск групп по фрагменту кода, без учёта регистра.
    Голый вариант «403» ищется ещё и как «с403» — так его обычно и пишут.
    """
    query = query.strip().lower()
    with_prefix = 'с' + query if query.isdigit() else query
    return conn.execute(
        """SELECT g.id, g.code, g.department, g.program, f.name AS faculty_name
             FROM groups_ g JOIN faculties f ON g.faculty_id = f.id
            WHERE pylower(g.code) LIKE ? OR pylower(g.code) LIKE ?
            ORDER BY g.code LIMIT ?""",
        (f'%{query}%', f'%{with_prefix}%', limit)
    ).fetchall()


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
    # Языковые потоки исключены: они всегда стоят на одной паре друг с
    # другом и иначе выглядели бы как предметы по выбору, которых человек
    # не выбирал
    rows = conn.execute(
        """SELECT date, pair_number, time_start, subject, subject_abbr, room, lesson_type, teacher
           FROM lessons
          WHERE group_id = ? AND subgroup = '' AND date >= date('now')
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


# === Бот: языковые потоки ===

def get_stream_subjects(conn, group_id: int) -> list:
    """Языки, которые есть у группы в потоках: [(предмет, занятий), ...]."""
    return [(r['subject'], r['n']) for r in conn.execute(
        """SELECT subject, COUNT(*) AS n FROM lessons
            WHERE group_id = ? AND subgroup != '' AND date >= date('now')
            GROUP BY subject ORDER BY n DESC""", (group_id,))]


def get_stream_teachers(conn, group_id: int, subject: str) -> list:
    """Преподаватели этого языка у группы: [(преподаватель, занятий), ...]."""
    return [(r['teacher'], r['n']) for r in conn.execute(
        """SELECT teacher, COUNT(*) AS n FROM lessons
            WHERE group_id = ? AND subgroup != '' AND subject = ?
              AND teacher != '' AND date >= date('now')
            GROUP BY teacher ORDER BY n DESC""", (group_id, subject))]


def set_user_stream(conn, chat_id: int, group_id: int, subject: str, teacher: str = ''):
    conn.execute(
        """INSERT INTO user_streams (chat_id, group_id, subject, teacher)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id, group_id) DO UPDATE SET
               subject = excluded.subject, teacher = excluded.teacher""",
        (chat_id, group_id, subject, teacher))
    conn.commit()


def clear_user_stream(conn, chat_id: int, group_id: int):
    conn.execute("DELETE FROM user_streams WHERE chat_id = ? AND group_id = ?",
                 (chat_id, group_id))
    conn.commit()


def resolve_user_stream(conn, chat_id: int, group_id: int) -> dict | None:
    """
    Сохранённый выбор потока — но только если он ещё к чему-то подходит.

    Здесь и живёт защита от «ломается раз в полгода»: если выбранного языка
    у группы больше нет (сменился семестр, студент перешёл на курс без
    языков), возвращаем None, и бот показывает все потоки, как будто выбора
    не было. Хуже нынешнего поведения не станет никогда.

    Преподаватель — второй уровень и необязательный: если он сменился,
    выбор сам скатывается до уровня языка, а не пропадает целиком.
    """
    row = conn.execute(
        "SELECT subject, teacher FROM user_streams WHERE chat_id = ? AND group_id = ?",
        (chat_id, group_id)).fetchone()
    if not row:
        return None

    subjects = {s for s, _ in get_stream_subjects(conn, group_id)}
    if row['subject'] not in subjects:
        return None          # язык устарел — фильтр молча выключается

    teacher = row['teacher'] or ''
    if teacher:
        teachers = {t for t, _ in get_stream_teachers(conn, group_id, row['subject'])}
        if teacher not in teachers:
            teacher = ''     # преподаватель сменился — остаёмся на языке

    return {'subject': row['subject'], 'teacher': teacher}


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
    """Статистика для /stats. Админ исключён из подсчётов."""
    from core.config import ADMIN_CHAT_ID
    admin_id = int(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else 0

    total = conn.execute(
        "SELECT COUNT(*) as c FROM users WHERE chat_id != ?", (admin_id,)
    ).fetchone()['c']
    active_7d = conn.execute(
        "SELECT COUNT(*) as c FROM users WHERE last_active > datetime('now', '-7 days') AND chat_id != ?",
        (admin_id,)
    ).fetchone()['c']
    today_actions = conn.execute(
        "SELECT COUNT(*) as c FROM activity_log WHERE timestamp > date('now') AND chat_id != ?",
        (admin_id,)
    ).fetchone()['c']
    ad_clicks = conn.execute(
        "SELECT COUNT(*) as c FROM activity_log WHERE action = 'ad_click' AND chat_id != ?",
        (admin_id,)
    ).fetchone()['c']
    top_groups = conn.execute(
        """SELECT group_code, COUNT(*) as cnt FROM users
           WHERE group_code IS NOT NULL AND group_code != '' AND chat_id != ?
           GROUP BY group_code ORDER BY cnt DESC LIMIT 10""",
        (admin_id,)
    ).fetchall()
    return {
        'total': total,
        'active_7d': active_7d,
        'today_actions': today_actions,
        'ad_clicks': ad_clicks,
        'top_groups': [(r['group_code'], r['cnt']) for r in top_groups],
    }