"""
Форматирование расписания для Telegram-сообщений.
"""

from datetime import date

# Дни недели на русском
WEEKDAYS_RU = {
    0: 'Понедельник',
    1: 'Вторник',
    2: 'Среда',
    3: 'Четверг',
    4: 'Пятница',
    5: 'Суббота',
    6: 'Воскресенье',
}

WEEKDAYS_SHORT = {
    0: 'Пн', 1: 'Вт', 2: 'Ср', 3: 'Чт', 4: 'Пт', 5: 'Сб', 6: 'Вс',
}

TYPE_EMOJI = {
    'Лк': '📗',
    'Сем': '📙',
    'Зч': '📝',
    'Экз': '🔴',
    'Пр': '📘',
    'Конс': '💬',
}


def format_date_header(d: date) -> str:
    """Красивый заголовок даты: 📅 Четверг, 6 марта"""
    months = [
        '', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
        'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'
    ]
    weekday = WEEKDAYS_RU[d.weekday()]
    return f"📅 <b>{weekday}, {d.day} {months[d.month]}</b>"


def format_lesson(lesson) -> str:
    """Форматировать одно занятие в строку."""
    emoji = TYPE_EMOJI.get(lesson['lesson_type'], '📌')
    type_str = lesson['lesson_type'] or ''
    room = lesson['room']

    # Используем аббревиатуру если есть, иначе полное название (обрезанное)
    name = lesson['subject_abbr'] or lesson['subject']
    if len(name) > 30:
        name = name[:27] + '...'

    return (
        f"  {emoji} <b>{lesson['pair_number']}</b> "
        f"({lesson['time_start']}–{lesson['time_end']}) "
        f"<b>{name}</b> [{type_str}] ауд.{room}"
    )


def format_day_schedule(lessons: list, d: date) -> str:
    """Форматировать расписание на один день."""
    if not lessons:
        weekday = WEEKDAYS_RU[d.weekday()]
        return f"📅 <b>{weekday}, {d.day}</b>\n  🎉 Нет занятий!"

    header = format_date_header(d)
    lines = [header]
    for l in lessons:
        lines.append(format_lesson(l))

    return '\n'.join(lines)


def format_week_schedule(days: dict) -> str:
    """
    Форматировать расписание на неделю.
    days: {date_obj: [lessons]}
    """
    if not days:
        return "📭 На эту неделю расписания нет"

    parts = []
    for d in sorted(days.keys()):
        parts.append(format_day_schedule(days[d], d))

    return '\n\n'.join(parts)


def format_subject_list(subjects: list[dict], selected: list[str]) -> str:
    """Форматировать список предметов для выбора."""
    if not subjects:
        return "✅ В расписании нет предметов по выбору — всё однозначно!"

    lines = ["<b>📋 Предметы по выбору</b>\n",
             "Нажми на предмет, чтобы добавить/убрать.\n"
             "Выбранные предметы отмечены ✅\n"]

    for s in subjects:
        check = '✅' if s['subject'] in selected else '⬜️'
        lines.append(f"  {check} {s['subject']}")

    lines.append("\nНевыбранные предметы не будут "
                 "показываться в расписании.")
    return '\n'.join(lines)
