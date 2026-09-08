"""
Функции БД для работы со студентами и преподавателями.
Отдельный модуль — не нужно трогать database.py.
"""


def ensure_tables(conn):
    """Создать таблицу students если её нет."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            site_id TEXT NOT NULL,
            full_name TEXT NOT NULL,
            short_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups_(id),
            UNIQUE(group_id, site_id)
        );
        CREATE INDEX IF NOT EXISTS idx_students_group ON students(group_id);
    """)
    conn.commit()


def get_groups_for_student_parse(conn) -> list:
    """Получить группы для парсинга: (id, code, site_id)."""
    return conn.execute(
        "SELECT id, code, site_id FROM groups_ WHERE site_id IS NOT NULL AND site_id != '' ORDER BY code"
    ).fetchall()


def save_students(conn, group_id: int, students: list):
    """Сохранить/обновить список студентов группы."""
    for s in students:
        conn.execute(
            """INSERT INTO students (group_id, site_id, full_name, short_name)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(group_id, site_id) DO UPDATE SET
                   full_name = excluded.full_name,
                   short_name = excluded.short_name""",
            (group_id, s['site_id'], s['full_name'], s.get('short_name', ''))
        )
    conn.commit()


def group_subject_coverage(conn, group_id: int) -> tuple:
    """
    (студентов в группе, из них с собранными предметами).
    Нужно для --resume: не переспрашивать сайт про уже собранные группы.
    """
    total = conn.execute(
        "SELECT COUNT(*) AS c FROM students WHERE group_id = ?", (group_id,)
    ).fetchone()['c']
    try:
        with_subjects = conn.execute(
            """SELECT COUNT(DISTINCT s.id) AS c FROM students s
               JOIN student_subjects ss ON ss.student_id = s.id
               WHERE s.group_id = ?""",
            (group_id,)
        ).fetchone()['c']
    except Exception:
        with_subjects = 0
    return total, with_subjects


def update_lesson_teachers(conn, teacher_updates: list) -> int:
    """Обновить teacher в занятиях. Возвращает кол-во обновлённых строк."""
    updated = 0
    for t in teacher_updates:
        cursor = conn.execute(
            """UPDATE lessons SET teacher = ?
               WHERE group_id = ? AND date = ? AND pair_number = ? AND subject = ?
               AND (teacher IS NULL OR teacher = '')""",
            (t['teacher'], t['group_id'], t['date'], t['pair_number'], t['subject'])
        )
        updated += cursor.rowcount
    conn.commit()
    return updated


def get_student_count(conn) -> int:
    """Сколько студентов в базе."""
    try:
        return conn.execute("SELECT COUNT(*) as c FROM students").fetchone()['c']
    except Exception:
        return 0


def get_students_by_group(conn, group_id: int) -> list:
    """Список студентов группы."""
    return conn.execute(
        "SELECT * FROM students WHERE group_id = ? ORDER BY full_name",
        (group_id,)
    ).fetchall()
    
def fill_teachers_from_same_subject(conn) -> int:
    """Дозаполнить преподавателей только для предметов по выбору (>1 предмет на пару)."""
    cursor = conn.execute("""
        UPDATE lessons SET teacher = (
            SELECT l2.teacher FROM lessons l2
            WHERE l2.group_id = lessons.group_id
              AND l2.date = lessons.date
              AND l2.subject = lessons.subject
              AND l2.teacher != ''
            LIMIT 1
        )
        WHERE teacher = ''
          AND EXISTS (
            SELECT 1 FROM lessons l2
            WHERE l2.group_id = lessons.group_id
              AND l2.date = lessons.date
              AND l2.subject = lessons.subject
              AND l2.teacher != ''
        )
          AND (SELECT COUNT(DISTINCT subject) FROM lessons l3
               WHERE l3.group_id = lessons.group_id
                 AND l3.date = lessons.date
                 AND l3.pair_number = lessons.pair_number
              ) > 1
    """)
    conn.commit()
    return cursor.rowcount

def get_students_by_name(conn, query: str, group_id: int = None, limit: int = 10) -> list:
    """
    Поиск студентов по началу фамилии, без учёта регистра.
    pylower обязателен: встроенный LOWER() кириллицу не трогает, и «иванова»
    не находила «Иванову».
    """
    pattern = f'{query.strip().lower()}%'
    if group_id:
        return conn.execute(
            """SELECT s.*, g.code as group_code FROM students s
               JOIN groups_ g ON s.group_id = g.id
               WHERE pylower(s.full_name) LIKE ? AND s.group_id = ?
               ORDER BY s.full_name LIMIT ?""",
            (pattern, group_id, limit)
        ).fetchall()
    return conn.execute(
        """SELECT s.*, g.code as group_code FROM students s
           JOIN groups_ g ON s.group_id = g.id
           WHERE pylower(s.full_name) LIKE ?
           ORDER BY s.full_name LIMIT ?""",
        (pattern, limit)
    ).fetchall()


def find_teachers_by_name(conn, query: str, limit: int = 10) -> list:
    """
    Поиск преподавателей по фрагменту фамилии, без учёта регистра.

    В lessons.teacher может лежать несколько человек через запятую
    («Осипова Н.Г., Елишев С.О.»), поэтому строки разбираются на отдельные
    имена, и в выдачу попадают только те, что действительно совпали.
    """
    q = query.strip().lower()
    if not q:
        return []

    rows = conn.execute(
        """SELECT DISTINCT teacher FROM lessons
            WHERE teacher IS NOT NULL AND teacher != ''
              AND pylower(teacher) LIKE ?
            ORDER BY teacher""",
        (f'%{q}%',)
    ).fetchall()

    seen = set()
    names = []
    for r in rows:
        for name in r['teacher'].split(','):
            name = name.strip()
            if name and q in name.lower() and name not in seen:
                seen.add(name)
                names.append(name)

    names.sort()
    return names[:limit]


def bind_student(conn, chat_id: int, student_id: int):
    """
    Привязать Telegram-аккаунт к студенту.

    Колонку users.student_id раньше добавлял ALTER прямо здесь, в try/except,
    то есть при каждой привязке. Теперь она заводится один раз при открытии
    соединения — см. core.database._migrate.
    """
    conn.execute("UPDATE users SET student_id = ? WHERE chat_id = ?", (student_id, chat_id))
    conn.commit()


def get_bound_student(conn, chat_id: int) -> dict | None:
    """Получить привязанного студента."""
    try:
        row = conn.execute(
            """SELECT s.*, g.code as group_code FROM users u
               JOIN students s ON u.student_id = s.id
               JOIN groups_ g ON s.group_id = g.id
               WHERE u.chat_id = ?""",
            (chat_id,)
        ).fetchone()
        return dict(row) if row else None
    except Exception:
        return None

def ensure_student_subjects_table(conn):
    """Таблица предметов студента."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS student_subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            UNIQUE(student_id, subject)
        );
        CREATE INDEX IF NOT EXISTS idx_student_subjects ON student_subjects(student_id);
    """)
    conn.commit()


def save_student_subjects(conn, student_db_id: int, subjects: list):
    """Сохранить предметы конкретного студента."""
    conn.execute("DELETE FROM student_subjects WHERE student_id = ?", (student_db_id,))
    for subj in subjects:
        conn.execute(
            "INSERT OR IGNORE INTO student_subjects (student_id, subject) VALUES (?, ?)",
            (student_db_id, subj)
        )
    conn.commit()

def apply_student_filter(conn, chat_id: int, student_id: int, group_id: int):
    """При привязке — скопировать предметы студента в user_subjects."""
    # Берём предметы студента
    rows = conn.execute(
        "SELECT subject FROM student_subjects WHERE student_id = ?",
        (student_id,)
    ).fetchall()

    if not rows:
        return 0

    student_subjects = {r['subject'] for r in rows}

    # Находим предметы по выбору в группе (конфликтные)
    from core.database import get_conflicting_subjects
    conflicts = get_conflicting_subjects(conn, group_id)
    conflict_subjects = {c['subject'] for c in conflicts}

    # Пересечение: предметы студента которые являются предметами по выбору
    to_select = student_subjects & conflict_subjects

    if not to_select:
        return 0

    # Очищаем старый выбор и ставим новый
    conn.execute(
        "DELETE FROM user_subjects WHERE chat_id = ? AND group_id = ?",
        (chat_id, group_id)
    )
    for subj in to_select:
        conn.execute(
            "INSERT OR IGNORE INTO user_subjects (chat_id, group_id, subject) VALUES (?, ?, ?)",
            (chat_id, group_id, subj)
        )
    conn.commit()
    return len(to_select)