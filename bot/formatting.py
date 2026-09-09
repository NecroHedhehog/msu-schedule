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

WEEKDAYS_SHORT = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс']

TYPE_EMOJI = {
    'Лк': '📗', 'Сем': '📙', 'Зч': '📝',
    'Экз': '🔴', 'Пр': '📘', 'Пз': '📘', 'Конс': '💬', 'Доп': '📎',
}


def format_date_header(d: date) -> str:
    weekday = WEEKDAYS_RU[d.weekday()]
    return f"📅 <b>{weekday}, {d.day} {MONTHS_RU[d.month]}</b>"


def format_slots(slots, limit: int = 2) -> str:
    """
    «пн 10:40, ср 12:20» — по этому человек и узнаёт своё занятие,
    когда один преподаватель ведёт два потока.
    """
    parts = [f"{WEEKDAYS_SHORT[wd]} {time}" for wd, time in slots[:limit]]
    if len(slots) > limit:
        parts.append('…')
    return ', '.join(parts)


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


def stream_number(subgroup: str) -> str:
    """«с101-3» → «3». Код группы человек и так знает."""
    return subgroup.rsplit('-', 1)[-1].strip() if '-' in subgroup else subgroup


def apply_stream_choice(streams: list, choice: dict = None) -> list:
    """
    Оставить только выбранный человеком поток.

    choice приходит из resolve_user_stream, то есть уже проверен на
    актуальность: если выбранного языка у группы больше нет, сюда придёт
    None и покажется всё.

    Пустой результат — нормально: значит сегодня язык этого человека
    не идёт, и блок показывать не надо.
    """
    if not choice:
        return list(streams)

    subject = choice.get('subject')
    teacher = (choice.get('teacher') or '').strip()
    subgroup = (choice.get('subgroup') or '').strip()

    return [l for l in streams
            if l['subject'] == subject
            and (not teacher or teacher in field(l, 'teacher'))
            and (not subgroup or field(l, 'subgroup') == subgroup)]


def format_streams(streams: list, choice: dict = None) -> str:
    """
    Блок потоков: сгруппирован по предмету.

    Плоским списком получалось до десяти строк на день — у группы бывает
    семь потоков, и английский идёт сразу у пяти преподавателей. Сгруппировав
    по предмету, человек сначала находит свой язык, а потом свой поток.
    Если поток выбран, остаётся одна строка.
    """
    if not streams:
        return ''

    had_any = bool(streams)
    streams = apply_stream_choice(streams, choice)
    if not streams:
        # Выбор есть, но сегодня этого языка нет — блок не нужен
        return '' if choice else ''

    by_subject = {}
    for l in streams:
        by_subject.setdefault(l['subject'], []).append(l)

    title = "🔤 <b>Ваш язык</b>" if choice else "🔤 <b>Не у всех</b>"
    lines = ["", "  ─────────", f"  {title}"]
    for subject in sorted(by_subject):
        lines.append(f"  · <b>{subject}</b>")

        seen = set()
        rows = sorted(by_subject[subject],
                      key=lambda x: (x['pair_number'], stream_number(field(x, 'subgroup'))))
        for l in rows:
            room = field(l, 'room') or '?'
            teacher = field(l, 'teacher')
            key = (l['pair_number'], l['time_start'], room, teacher)
            if key in seen:
                continue
            seen.add(key)

            tail = f" {teacher}" if teacher else ''
            lines.append(
                f"     {l['pair_number']} ({l['time_start']}–{l['time_end']}) "
                f"{room}{tail}  <i>[{stream_number(field(l, 'subgroup'))}]</i>")

    lines.append("  <i>/язык — изменить</i>" if choice
                 else "  <i>выбрать свой — /язык</i>")
    return '\n'.join(lines)


def pairs_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return 'пара'
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return 'пары'
    return 'пар'


def group_by_pair(lessons: list) -> list:
    """[(номер пары, [занятия]), ...] по возрастанию. На одной паре может
    стоять несколько предметов — это предметы по выбору."""
    by_pair = {}
    for l in lessons:
        by_pair.setdefault(l['pair_number'], []).append(l)
    return sorted(by_pair.items())


GAP_MIN_MINUTES = 30


def minutes_of(t: str) -> int:
    h, m = t.split(':')
    return int(h) * 60 + int(m)


def format_duration(minutes: int) -> str:
    """80 → «1 ч 20 мин»."""
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    return f"{m} мин"


def format_gap(prev_items: list, next_items: list, free: int):
    """
    Строка окна между парами или None, если окна нет.

    Время берётся из самих занятий — конец предыдущего и начало следующего, —
    а не из таблицы пар. Так оно остаётся верным и там, где у группы
    своё расписание звонков.

    Пропущенных пар может не быть, а окно быть: по средам МФК начинается
    через час с лишним после третьей пары, хотя номера идут подряд. Таких
    дней в базе 265 — промолчать о них значит соврать.
    """
    end = max(l['time_end'] for l in prev_items)
    start = min(l['time_start'] for l in next_items)

    if free:
        return f"  ⌛ <i>окно {free} {pairs_word(free)} · {end}–{start}</i>"

    idle = minutes_of(start) - minutes_of(end)
    if idle >= GAP_MIN_MINUTES:
        return f"  ⌛ <i>перерыв {format_duration(idle)} · {end}–{start}</i>"
    return None


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
                        data_range: tuple = None, stream_choice: dict = None) -> str:
    header = format_date_header(d)
    main, streams = split_streams(lessons)
    streams_text = format_streams(streams, stream_choice)

    if not main and not streams_text:
        text = f"{header}\n  {empty_day_reason(d, data_range)}"
    elif not main:
        # У группы пар нет, а языковой поток есть — так бывает
        text = f"{header}\n  —" + streams_text
    else:
        # Пары, которые у человека заняты. Выбранный языковой поток тоже
        # занимает пару, хотя показывается отдельным блоком: если его
        # не учесть, бот покажет окно там, где человек сидит на английском.
        occupied = {l['pair_number'] for l in main}
        if stream_choice:
            occupied |= {l['pair_number']
                         for l in apply_stream_choice(streams, stream_choice)}

        lines = [header]
        groups = group_by_pair(main)
        for i, (pair, items) in enumerate(groups):
            if i:
                prev_pair, prev_items = groups[i - 1]
                between = [p for p in range(prev_pair + 1, pair)]
                # Окно показываем, только если промежуток пуст целиком:
                # частично занятый — не окно, а лишний повод для путаницы
                if not any(p in occupied for p in between):
                    gap = format_gap(prev_items, items, len(between))
                    if gap:
                        lines.append(gap)
            for l in items:
                lines.append(format_lesson(l))
        text = '\n'.join(lines) + streams_text

    if with_ad and AD_TEASER:
        text += f"\n\n{'─' * 20}\n{AD_TEASER}"

    return text


def format_week_schedule(days: dict, data_range: tuple = None,
                         stream_choice: dict = None) -> str:
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
                                             data_range=data_range,
                                             stream_choice=stream_choice))
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
