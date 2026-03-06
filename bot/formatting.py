"""
Форматирование расписания для Telegram-сообщений.
"""

from datetime import date

WEEKDAYS_RU = {
    0: 'Понедельник', 1: 'Вторник', 2: 'Среда',
    3: 'Четверг', 4: 'Пятница', 5: 'Суббота', 6: 'Воскресенье',
}

MONTHS_RU = [
    '', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
]

TYPE_EMOJI = {
    'Лк': '📗', 'Сем': '📙', 'Зч': '📝',
    'Экз': '🔴', 'Пр': '📘', 'Конс': '💬',
}


def format_date_header(d: date) -> str:
    weekday = WEEKDAYS_RU[d.weekday()]
    return f"📅 <b>{weekday}, {d.day} {MONTHS_RU[d.month]}</b>"


def format_lesson(lesson) -> str:
    emoji = TYPE_EMOJI.get(lesson['lesson_type'], '📌')
    name = lesson['subject_abbr'] or lesson['subject']
    if len(name) > 30:
        name = name[:27] + '...'
    return (
        f"  {emoji} <b>{lesson['pair_number']}</b> "
        f"({lesson['time_start']}–{lesson['time_end']}) "
        f"<b>{name}</b> [{lesson['lesson_type']}] ауд.{lesson['room']}"
    )


def format_day_schedule(lessons: list, d: date) -> str:
    header = format_date_header(d)
    if not lessons:
        return f"{header}\n  🎉 Нет занятий!"
    lines = [header]
    for l in lessons:
        lines.append(format_lesson(l))
    return '\n'.join(lines)


def format_week_schedule(days: dict) -> str:
    if not any(days.values()):
        return "📭 На эту неделю занятий нет"
    parts = []
    for d in sorted(days.keys()):
        if d.weekday() < 6:  # Пн-Сб
            parts.append(format_day_schedule(days[d], d))
    return '\n\n'.join(parts)
