"""
Telegram-бот для расписания МГУ.

Возможности:
  - Выбор группы кнопками (направление → курс → группа)
  - Расписание на сегодня / завтра / неделю с навигацией
  - Выбор своих предметов (фильтр предметов по выбору)
  - Постоянное меню с кнопками
"""

import asyncio
import hashlib
import logging
import re
from datetime import date, timedelta, datetime
from collections import defaultdict

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.enums import ParseMode

from core.config import BOT_TOKEN
from core.database import (
    get_connection, get_user_group, set_user_group,
    get_lessons_for_date, get_lessons_for_week,
    get_conflicting_subjects, get_user_subjects, toggle_user_subject,
)
from bot.formatting import format_day_schedule, format_week_schedule

logging.basicConfig(level=logging.INFO)
router = Router()


# === Постоянная клавиатура ===

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Завтра")],
        [KeyboardButton(text="🗓 Неделя"), KeyboardButton(text="📋 Предметы")],
        [KeyboardButton(text="👥 Сменить группу")],
    ],
    resize_keyboard=True,
)


# === Утилиты ===

def subject_hash(subject: str) -> str:
    return hashlib.md5(subject.encode()).hexdigest()[:10]


def normalize_group_query(text: str) -> str:
    """Нормализация ввода: английская c → русская с, убираем лишнее."""
    text = text.strip().lower()
    # Английская 'c' → русская 'с' в начале
    if text.startswith('c') and len(text) > 1 and text[1:2].isdigit():
        text = 'с' + text[1:]
    # Английские pp → русские пп
    if text.startswith('pp'):
        text = 'пп' + text[2:]
    return text


def filter_lessons(lessons: list, user_subjects: list[str]) -> list:
    """
    Фильтрация занятий по выбранным предметам.
    Если user_subjects пуст — возвращает всё.
    Если заполнен — показывает только выбранные предметы.
    """
    if not user_subjects:
        return list(lessons)
    return [l for l in lessons if l['subject'] in user_subjects]


def get_schedule_for_date(group_id: int, d: date, chat_id: int) -> list:
    """Получить отфильтрованное расписание на дату."""
    conn = get_connection()
    lessons = get_lessons_for_date(conn, group_id, d.strftime('%Y-%m-%d'))
    user_subj = get_user_subjects(conn, chat_id, group_id)
    conn.close()
    return filter_lessons(lessons, user_subj)


def get_week_days(group_id: int, monday: date, chat_id: int) -> dict:
    """Получить расписание на неделю с фильтрацией."""
    sunday = monday + timedelta(days=6)
    conn = get_connection()
    all_lessons = get_lessons_for_week(
        conn, group_id,
        monday.strftime('%Y-%m-%d'),
        sunday.strftime('%Y-%m-%d'),
    )
    user_subj = get_user_subjects(conn, chat_id, group_id)
    conn.close()

    filtered = filter_lessons(all_lessons, user_subj)

    days = defaultdict(list)
    for l in filtered:
        d = datetime.strptime(l['date'], '%Y-%m-%d').date()
        days[d].append(l)

    # Добавляем пустые будни
    for i in range(6):
        d = monday + timedelta(days=i)
        if d not in days:
            days[d] = []

    return dict(days)


# === Навигация по неделям ===

def week_nav_keyboard(monday: date) -> InlineKeyboardMarkup:
    """Кнопки навигации по неделям."""
    prev_monday = monday - timedelta(days=7)
    next_monday = monday + timedelta(days=7)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="← Пред. неделя",
            callback_data=f"week:{prev_monday.isoformat()}",
        ),
        InlineKeyboardButton(
            text="След. неделя →",
            callback_data=f"week:{next_monday.isoformat()}",
        ),
    ]])


def day_nav_keyboard(d: date) -> InlineKeyboardMarkup:
    """Кнопки навигации по дням."""
    prev_day = d - timedelta(days=1)
    next_day = d + timedelta(days=1)
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="← Вчера",
            callback_data=f"day:{prev_day.isoformat()}",
        ),
        InlineKeyboardButton(
            text="Завтра →",
            callback_data=f"day:{next_day.isoformat()}",
        ),
    ]])


# === Проверка группы ===

async def check_group(message_or_callback) -> dict | None:
    """Проверить что у пользователя выбрана группа."""
    if isinstance(message_or_callback, CallbackQuery):
        chat_id = message_or_callback.message.chat.id
        answer = message_or_callback.message.answer
    else:
        chat_id = message_or_callback.chat.id
        answer = message_or_callback.answer

    conn = get_connection()
    user = get_user_group(conn, chat_id)
    conn.close()

    if not user:
        await answer(
            "⚠️ Сначала выбери группу!\n"
            "Нажми <b>👥 Сменить группу</b> или напиши номер группы.",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD,
        )
        return None
    return user


# === /start ===

@router.message(CommandStart())
async def cmd_start(message: Message):
    conn = get_connection()
    user = get_user_group(conn, message.chat.id)
    conn.close()

    if user:
        await message.answer(
            f"👋 С возвращением! Твоя группа: <b>{user['group_code']}</b>\n\n"
            f"Используй кнопки внизу 👇",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await message.answer(
            "👋 Привет! Я бот расписания МГУ.\n\n"
            "Для начала выбери свою группу:",
            parse_mode=ParseMode.HTML,
        )
        await show_department_selection(message)


# === Выбор группы кнопками ===

async def show_department_selection(message: Message):
    """Показать выбор направления."""
    conn = get_connection()
    # Получаем уникальные комбинации department + program
    rows = conn.execute(
        """SELECT DISTINCT g.department, g.program
           FROM groups_ g
           JOIN faculties f ON g.faculty_id = f.id
           WHERE g.department != '' AND g.program != ''
           ORDER BY g.department, g.program"""
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer(
            "База пуста. Сначала запусти парсер:\n"
            "<code>python run_parser.py socio</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    buttons = []
    seen = set()
    for r in rows:
        dept = r['department']
        prog = r['program']
        label = f"{dept} — {prog}" if prog else dept
        key = f"{dept}|{prog}"
        if key not in seen:
            seen.add(key)
            buttons.append([
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"dept:{dept}|{prog}",
                )
            ])

    # Добавляем кнопку "ввести вручную"
    buttons.append([
        InlineKeyboardButton(text="✏️ Ввести номер группы", callback_data="manual_input")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(
        "📚 Выбери направление:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == 'manual_input')
async def on_manual_input(callback: CallbackQuery):
    await callback.message.edit_text(
        "Напиши номер группы, например:\n"
        "<b>с403</b>, <b>403</b>, <b>пп201</b>, <b>мг52МКПП</b>",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data == 'back_to_dept')
async def on_back_to_dept(callback: CallbackQuery):
    conn = get_connection()
    rows = conn.execute(
        """SELECT DISTINCT g.department, g.program
           FROM groups_ g WHERE g.department != '' AND g.program != ''
           ORDER BY g.department, g.program"""
    ).fetchall()
    conn.close()

    buttons = []
    seen = set()
    for r in rows:
        key = f"{r['department']}|{r['program']}"
        label = f"{r['department']} — {r['program']}" if r['program'] else r['department']
        if key not in seen:
            seen.add(key)
            buttons.append([
                InlineKeyboardButton(text=label, callback_data=f"dept:{key}")
            ])
    buttons.append([
        InlineKeyboardButton(text="✏️ Ввести номер группы", callback_data="manual_input")
    ])

    await callback.message.edit_text(
        "📚 Выбери направление:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


def detect_course(code: str) -> int:
    """Определить номер курса из кода группы: с403 → 4, пп201 → 2, мг52МКПП → 1."""
    import re
    # Убираем буквенный префикс, берём первую цифру
    m = re.search(r'\d', code)
    if not m:
        return 0
    first_digit = int(m.group())
    # Для магистратуры: мг5x = 1 курс, мг6x = 2 курс
    if code.lower().startswith('мг') or code.lower().startswith('mg'):
        return first_digit - 4  # 5→1, 6→2
    return first_digit


@router.callback_query(F.data.startswith('dept:'))
async def on_department_select(callback: CallbackQuery):
    """Выбрано направление → показать курсы."""
    parts = callback.data.split(':', 1)[1].split('|')
    department = parts[0]
    program = parts[1] if len(parts) > 1 else ''

    conn = get_connection()
    groups = conn.execute(
        "SELECT id, code FROM groups_ WHERE department = ? AND program = ? ORDER BY code",
        (department, program)
    ).fetchall()
    conn.close()

    # Группируем по курсам
    courses = {}  # {номер_курса: [группы]}
    for g in groups:
        c = detect_course(g['code'])
        if c not in courses:
            courses[c] = []
        courses[c].append(g)

    is_mag = department.lower().startswith('маг')

    buttons = []
    for c in sorted(courses.keys()):
        if c <= 0:
            label = f"Группы ({len(courses[c])})"
        elif is_mag:
            label = f"{c} курс маг. ({len(courses[c])} гр.)"
        else:
            label = f"{c} курс ({len(courses[c])} гр.)"
        buttons.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"course:{department}|{program}|{c}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="← Назад", callback_data="back_to_dept")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(
        f"📚 {department} — {program}\nВыбери курс:",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith('course:'))
async def on_course_select(callback: CallbackQuery):
    """Выбран курс → показать группы."""
    parts = callback.data.split(':', 1)[1].split('|')
    department = parts[0]
    program = parts[1]
    target_course = int(parts[2])

    conn = get_connection()
    all_groups = conn.execute(
        "SELECT id, code FROM groups_ WHERE department = ? AND program = ? ORDER BY code",
        (department, program)
    ).fetchall()
    conn.close()

    rows = [g for g in all_groups if detect_course(g['code']) == target_course]

    buttons = []
    for r in rows:
        buttons.append([
            InlineKeyboardButton(
                text=r['code'],
                callback_data=f"grp:{r['id']}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="← Назад",
                             callback_data=f"dept:{department}|{program}")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(
     f"📚 {department} — {program} — {target_course} курс\nВыбери группу:",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith('grp:'))
async def on_group_select(callback: CallbackQuery):
    """Группа выбрана."""
    group_id = int(callback.data.split(':')[1])
    conn = get_connection()
    set_user_group(conn, callback.message.chat.id, group_id)
    user = get_user_group(conn, callback.message.chat.id)

    # Проверить есть ли предметы по выбору
    conflicts = get_conflicting_subjects(conn, user['group_id'])
    conn.close()

    text = (
        f"✅ Группа: <b>{user['group_code']}</b>\n"
        f"   {user['faculty_name']}, {user['department']}\n"
    )

    if conflicts:
        text += (
            f"\n⚠️ В расписании <b>{len(conflicts)}</b> предметов по выбору.\n"
            f"Нажми <b>📋 Предметы</b>, чтобы отметить свои — "
            f"тогда в расписании будут только они.\n"
        )

    text += "\nИспользуй кнопки внизу 👇"

    await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
    await callback.message.answer("Готово!", reply_markup=MAIN_KEYBOARD)
    await callback.answer()


# === Расписание ===

@router.message(F.text == "📅 Сегодня")
@router.message(Command('сегодня', 'today'))
async def cmd_today(message: Message):
    user = await check_group(message)
    if not user:
        return

    d = date.today()
    lessons = get_schedule_for_date(user['group_id'], d, message.chat.id)

    text = f"👥 <b>{user['group_code']}</b>\n\n"
    text += format_day_schedule(lessons, d)

    # В выходные подсказываем про следующую неделю
    if d.weekday() >= 5 and not lessons:
        text += "\n\nНажми <b>🗓 Неделя</b> — покажу следующую."

    await message.answer(
        text, parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(F.text == "📆 Завтра")
@router.message(Command('завтра', 'tomorrow'))
async def cmd_tomorrow(message: Message):
    user = await check_group(message)
    if not user:
        return

    d = date.today() + timedelta(days=1)
    lessons = get_schedule_for_date(user['group_id'], d, message.chat.id)

    text = f"👥 <b>{user['group_code']}</b>\n\n"
    text += format_day_schedule(lessons, d)

    await message.answer(
        text, parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(F.text == "🗓 Неделя")
@router.message(Command('неделя', 'week'))
async def cmd_week(message: Message):
    user = await check_group(message)
    if not user:
        return

    today = date.today()
    monday = today - timedelta(days=today.weekday())

    # В выходные показываем следующую неделю
    if today.weekday() >= 5:
        monday = monday + timedelta(days=7)

    await send_week(message, user, monday)


async def send_week(message_or_callback, user: dict, monday: date):
    """Отправить расписание на неделю."""
    if isinstance(message_or_callback, CallbackQuery):
        chat_id = message_or_callback.message.chat.id
    else:
        chat_id = message_or_callback.chat.id

    days = get_week_days(user['group_id'], monday, chat_id)
    sunday = monday + timedelta(days=6)

    header = (
        f"👥 <b>{user['group_code']}</b>\n"
        f"📅 Неделя: {monday.strftime('%d.%m')} — {sunday.strftime('%d.%m')}\n\n"
    )

    text = header + format_week_schedule(days)
    keyboard = week_nav_keyboard(monday)

    # Telegram лимит 4096 символов
    if len(text) > 4000:
        # Разбиваем по дням
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.message.edit_text(
                header + "⬇️ Расписание по дням:",
                parse_mode=ParseMode.HTML,
            )
            send = message_or_callback.message.answer
        else:
            await message_or_callback.answer(
                header + "⬇️ Расписание по дням:",
                parse_mode=ParseMode.HTML,
                reply_markup=MAIN_KEYBOARD,
            )
            send = message_or_callback.answer

        for d in sorted(days.keys()):
            if d.weekday() < 6:
                await send(
                    format_day_schedule(days[d], d),
                    parse_mode=ParseMode.HTML,
                )
        await send("Навигация:", reply_markup=keyboard)
    else:
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.message.edit_text(
                text, parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        else:
            await message_or_callback.answer(
                text, parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )


@router.callback_query(F.data.startswith('week:'))
async def on_week_navigate(callback: CallbackQuery):
    """Навигация по неделям."""
    user = await check_group(callback)
    if not user:
        return

    date_str = callback.data.split(':')[1]
    monday = datetime.strptime(date_str, '%Y-%m-%d').date()

    await send_week(callback, user, monday)
    await callback.answer()


@router.callback_query(F.data.startswith('day:'))
async def on_day_navigate(callback: CallbackQuery):
    """Навигация по дням."""
    user = await check_group(callback)
    if not user:
        return

    date_str = callback.data.split(':')[1]
    d = datetime.strptime(date_str, '%Y-%m-%d').date()
    lessons = get_schedule_for_date(
        user['group_id'], d, callback.message.chat.id
    )

    text = f"👥 <b>{user['group_code']}</b>\n\n"
    text += format_day_schedule(lessons, d)

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=day_nav_keyboard(d),
    )
    await callback.answer()


# === Выбор предметов ===

@router.message(F.text == "📋 Предметы")
@router.message(Command('предметы', 'subjects'))
async def cmd_subjects(message: Message):
    user = await check_group(message)
    if not user:
        return

    conn = get_connection()
    conflicts = get_conflicting_subjects(conn, user['group_id'])
    selected = get_user_subjects(conn, message.chat.id, user['group_id'])
    conn.close()

    if not conflicts:
        await message.answer(
            "✅ В расписании нет предметов по выбору!",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    buttons = []
    for s in conflicts:
        check = '✅' if s['subject'] in selected else '⬜️'
        name = s['subject']
        if len(name) > 40:
            name = name[:37] + '...'
        buttons.append([
            InlineKeyboardButton(
                text=f"{check} {name}",
                callback_data=f"subj:{subject_hash(s['subject'])}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="🔄 Сбросить всё", callback_data="subj:reset"),
        InlineKeyboardButton(text="✅ Готово", callback_data="subj:done"),
    ])

    selected_count = len(selected)
    total_count = len(conflicts)

    await message.answer(
        f"📋 <b>Предметы по выбору</b> ({user['group_code']})\n\n"
        f"Выбрано: {selected_count} из {total_count}\n\n"
        f"Нажми на предмет чтобы добавить/убрать.\n"
        f"В расписании будут только отмеченные ✅\n"
        f"Если ничего не выбрано — показывается всё.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith('subj:'))
async def on_subject_toggle(callback: CallbackQuery):
    data = callback.data.split(':', 1)[1]

    if data == 'done':
        conn = get_connection()
        user = get_user_group(conn, callback.message.chat.id)
        selected = get_user_subjects(conn, callback.message.chat.id, user['group_id']) if user else []
        conn.close()

        count = len(selected)
        if count > 0:
            text = f"✅ Сохранено! Выбрано предметов: {count}\nВ расписании будут только они."
        else:
            text = "✅ Фильтр сброшен — будут показаны все предметы."

        await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
        await callback.answer()
        return

    conn = get_connection()
    user = get_user_group(conn, callback.message.chat.id)
    if not user:
        await callback.answer("Сначала выбери группу!")
        conn.close()
        return

    if data == 'reset':
        # Удалить все выбранные предметы
        conn.execute(
            "DELETE FROM user_subjects WHERE chat_id = ? AND group_id = ?",
            (callback.message.chat.id, user['group_id'])
        )
        conn.commit()
        await callback.answer("Фильтр сброшен!")
    else:
        # Найти предмет по хешу и переключить
        conflicts = get_conflicting_subjects(conn, user['group_id'])
        target = None
        for s in conflicts:
            if subject_hash(s['subject']) == data:
                target = s
                break

        if not target:
            await callback.answer("Предмет не найден")
            conn.close()
            return

        is_selected = toggle_user_subject(
            conn, callback.message.chat.id, user['group_id'], target['subject']
        )
        status = "добавлен ✅" if is_selected else "убран ⬜️"
        await callback.answer(f"Предмет {status}")

    # Обновить кнопки
    conflicts = get_conflicting_subjects(conn, user['group_id'])
    selected = get_user_subjects(conn, callback.message.chat.id, user['group_id'])
    conn.close()

    buttons = []
    for s in conflicts:
        check = '✅' if s['subject'] in selected else '⬜️'
        name = s['subject']
        if len(name) > 40:
            name = name[:37] + '...'
        buttons.append([
            InlineKeyboardButton(
                text=f"{check} {name}",
                callback_data=f"subj:{subject_hash(s['subject'])}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="🔄 Сбросить всё", callback_data="subj:reset"),
        InlineKeyboardButton(text="✅ Готово", callback_data="subj:done"),
    ])

    selected_count = len(selected)
    total_count = len(conflicts)

    await callback.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# === Сменить группу ===

@router.message(F.text == "👥 Сменить группу")
@router.message(Command('группа', 'group'))
async def cmd_change_group(message: Message):
    await show_department_selection(message)


# === Текстовый поиск группы ===

@router.message(Command('помощь', 'help'))
async def cmd_help(message: Message):
    await message.answer(
        "📌 <b>Как пользоваться:</b>\n\n"
        "Используй кнопки внизу экрана.\n\n"
        "📅 Сегодня — расписание на сегодня\n"
        "📆 Завтра — на завтра\n"
        "🗓 Неделя — на неделю (с навигацией)\n"
        "📋 Предметы — выбрать свои предметы\n"
        "👥 Сменить группу — выбрать другую группу\n\n"
        "Также можно просто написать номер группы: <b>403</b>, <b>с403</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(F.text & ~F.text.startswith('/'))
async def on_text_message(message: Message):
    """Обработка текстовых сообщений — поиск группы."""
    text = message.text.strip()

    # Игнорируем кнопки меню (они обработаны выше)
    if text in ("📅 Сегодня", "📆 Завтра", "🗓 Неделя",
                "📋 Предметы", "👥 Сменить группу"):
        return

    query = normalize_group_query(text)

    # Если ввели просто цифры — ищем с префиксом "с"
    if query.isdigit():
        query_with_prefix = 'с' + query
    else:
        query_with_prefix = query

    conn = get_connection()
    rows = conn.execute(
        """SELECT g.id, g.code, g.department, g.program,
                  f.name as faculty_name
           FROM groups_ g
           JOIN faculties f ON g.faculty_id = f.id
           WHERE LOWER(g.code) LIKE ? OR LOWER(g.code) LIKE ?
           ORDER BY g.code
           LIMIT 20""",
        (f'%{query}%', f'%{query_with_prefix}%')
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer(
            f"🔍 Группа «{text}» не найдена.\n"
            "Попробуй кнопку <b>👥 Сменить группу</b> для выбора.",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if len(rows) == 1:
        conn = get_connection()
        set_user_group(conn, message.chat.id, rows[0]['id'])
        user = get_user_group(conn, message.chat.id)
        conn.close()

        await message.answer(
            f"✅ Группа: <b>{user['group_code']}</b>\n"
            f"   {user['faculty_name']}, {user['department']}",
            parse_mode=ParseMode.HTML,
            reply_markup=MAIN_KEYBOARD,
        )
        return

    buttons = []
    for g in rows:
        label = f"{g['code']} ({g['department']}, {g['program']})"
        buttons.append([
            InlineKeyboardButton(text=label, callback_data=f"grp:{g['id']}")
        ])

    await message.answer(
        f"🔍 Найдено {len(rows)} групп:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# === Запуск ===

async def main():
    if not BOT_TOKEN or BOT_TOKEN == 'your_telegram_bot_token_here':
        print("❌ Укажи BOT_TOKEN в файле .env!")
        print("   Получи токен у @BotFather в Telegram")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    print("🤖 Бот запущен! Нажми Ctrl+C для остановки.")
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
