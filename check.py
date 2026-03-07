from core.database import get_connection
conn = get_connection()
total = conn.execute("SELECT COUNT(*) FROM lessons WHERE teacher != ''").fetchone()[0]
all_lessons = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
print(f"С преподавателем: {total} из {all_lessons} ({100*total//all_lessons}%)")
conn.close()