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
    'Экз': '🔴', 'Пр': '📘', 'Пз': '📘', 'Конс': '💬', 'Доп': '📎',
}


def format_date_header(d: date) -> str:
    weekday = WEEKDAYS_RU[d.weekday()]
    return f"📅 <b>{weekday}, {d.day} {MONTHS_RU[d.month]}</b>"


def format_lesson(lesson) -> str:
    """Форматировать занятие: две строки — пара + преподаватель."""
    emoji = TYPE_EMOJI.get(lesson['lesson_type'], '📌')
    name = lesson['subject_abbr'] or lesson['subject']
    if len(name) > 25:
        name = name[:22] + '...'

    room = lesson['room'] or '?'
    type_short = lesson['lesson_type'] or ''
    teacher = lesson['teacher'] if lesson['teacher'] else ''

    line1 = (
        f"  {emoji} <b>{lesson['pair_number']}</b> "
        f"({lesson['time_start']}–{lesson['time_end']}) "
        f"<b>{name}</b> {room} [{type_short}]"
    )

    if teacher:
        return f"{line1}\n     {teacher}"
    return line1


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
        text += f"\n\n{'─' * 20}\n{AD_TEASER}"

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
        text += f"\n\n{'─' * 20}\n{AD_TEASER}"

    return text


def format_subject_button(subject_data: dict) -> str:
    """Текст кнопки предмета: аббревиатура + преподаватель."""
    abbr = subject_data.get('subject_abbr') or subject_data['subject']
    teacher = subject_data.get('teacher', '')

    if len(abbr) > 15:
        abbr = abbr[:12] + '...'

    if teacher:
        label = f"{abbr} — {teacher}"
    else:
        label = abbr

    if len(label) > 40:
        label = label[:37] + '...'

    return label
