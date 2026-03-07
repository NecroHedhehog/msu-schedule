"""Форматирование расписания для Telegram."""

from datetime import date
from core.config import AD_TEASER

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


def format_day_schedule(lessons: list, d: date, with_ad: bool = True) -> str:
    header = format_date_header(d)
    if not lessons:
        text = f"{header}\n  🎉 Нет занятий!"
    else:
        lines = [header]
        for l in lessons:
            lines.append(format_lesson(l))
        text = '\n'.join(lines)

    if with_ad and AD_TEASER:
        text += f"\n\n{AD_TEASER}"

    return text


def format_week_schedule(days: dict) -> str:
    if not any(days.values()):
        return "📭 На эту неделю занятий нет"
    parts = []
    for d in sorted(days.keys()):
        if d.weekday() < 6:
            parts.append(format_day_schedule(days[d], d, with_ad=False))
    text = '\n\n'.join(parts)

    if AD_TEASER:
        text += f"\n\n{AD_TEASER}"

    return text
