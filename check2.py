from core.database import get_connection
from core.db_students import get_students_by_group

conn = get_connection()

group = conn.execute("SELECT id FROM groups_ WHERE code = 'с401'").fetchone()
students = get_students_by_group(conn, group['id'])
print(f"Студентов в с401: {len(students)}")

for subj in ['СоцМ(ая)', 'СЛАГПая']:
    row = conn.execute("""
        SELECT COUNT(*) as c FROM lessons l
        JOIN groups_ g ON l.group_id = g.id
        WHERE g.code = 'с401' AND l.subject_abbr = ? AND l.lesson_type = 'Сем'
        AND l.teacher != ''
    """, (subj,)).fetchone()
    print(f"  {subj} [Сем] с преподом: {row['c']}")

conn.close()