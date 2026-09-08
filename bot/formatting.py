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


def field(lesson, name: str, default=''):
    """
    Достать поле и из sqlite3.Row, и из обычного словаря.
    Row не умеет .get и кидает IndexError на отсутствующий ключ.
    """
    try:
        value = lesson[name]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def split_streams(lessons: list) -> tuple:
    """
    Разделить занятия группы и занятия языковых потоков.

    Поток нельзя показывать в общем списке: у группы одновременно идут
    французский, немецкий и три английских, и человеку это выглядит как
    пять пар в одно время. Их место — отдельным блоком «не у всех».
    """
    main, streams = [], []
    for l in lessons:
        (streams if field(l, 'subgroup') else main).append(l)
    return main, streams


def format_streams(streams: list) -> str:
    """Блок языковых потоков: человек сам знает, в каком он."""
    if not streams:
        return ''

    lines = ["\n  ─────────", "  🔤 <b>Не у всех</b> — языковые потоки:"]
    for l in sorted(streams, key=lambda x: (x['pair_number'], field(x, 'subgroup'))):
        emoji = TYPE_EMOJI.get(field(l, 'lesson_type'), '📌')
        name = field(l, 'subject_abbr') or l['subject']
        if len(name) > 22:
            name = name[:19] + '...'
        room = field(l, 'room') or '?'
        teacher = field(l, 'teacher')
        tail = f" · {teacher}" if teacher else ''
        lines.append(
            f"  {emoji} {l['pair_number']} ({l['time_start']}–{l['time_end']}) "
            f"<b>{name}</b> {room}{tail}  <i>{field(l, 'subgroup')}</i>")
    return '\n'.join(lines)


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


def empty_day_reason(d: date, data_range: tuple = None) -> str:
    """
    Почему в этом дне пусто. Раньше выходной, каникулы и «парсер сюда
    не доходил» выглядели одинаково — праздничным «Нет занятий».

    data_range: (min_date, max_date) из get_date_range(), ISO-строки.
    """
    min_d, max_d = data_range or (None, None)

    if min_d and max_d:
        first = date.fromisoformat(min_d)
        last = date.fromisoformat(max_d)
        if d > last:
            return "📭 Расписание на этот день ещё не выложено."
        if d < first:
            return "📭 Данных за этот день нет."

    if d.weekday() == 6:
        return "🎉 Воскресенье, занятий нет."
    return "🎉 Нет занятий!"


def format_day_schedule(lessons: list, d: date, with_ad: bool = True,
                        data_range: tuple = None) -> str:
    header = format_date_header(d)
    main, streams = split_streams(lessons)

    if not main and not streams:
        text = f"{header}\n  {empty_day_reason(d, data_range)}"
    elif not main:
        # У группы пар нет, а языковой поток есть — так бывает
        text = f"{header}\n  —" + format_streams(streams)
    else:
        lines = [header]
        for l in main:
            lines.append(format_lesson(l))
        text = '\n'.join(lines) + format_streams(streams)

    if with_ad and AD_TEASER:
        text += f"\n\n{'─' * 20}\n{AD_TEASER}"

    return text


def format_week_schedule(days: dict, data_range: tuple = None) -> str:
    if not any(days.values()):
        week = sorted(days.keys())
        if week and data_range and data_range[1]:
            if week[0] > date.fromisoformat(data_range[1]):
                return "📭 Расписание на эту неделю ещё не выложено"
        return "📭 На эту неделю занятий нет"
    parts = []
    for d in sorted(days.keys()):
        if d.weekday() < 6:
            parts.append(format_day_schedule(days[d], d, with_ad=False,
                                             data_range=data_range))
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
