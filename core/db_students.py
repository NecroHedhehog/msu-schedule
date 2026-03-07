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