"""
Telegram-бот расписания МГУ.
Выбор группы кнопками, расписание, фильтр предметов, аналитика, реклама.
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

from core.config import BOT_TOKEN, ADMIN_CHAT_ID, AD_FULL_TEXT, AD_BUTTON_LABEL
from core.database import (
    get_connection, get_user_group, set_user_group,
    get_lessons_for_date, get_lessons_for_week, get_date_range,
    get_conflicting_subjects, get_user_subjects, toggle_user_subject,
    track_user, log_action, get_stats,
)
from bot.formatting import format_day_schedule, format_week_schedule, format_subject_button
from core.db_students import get_students_by_name, bind_student, get_bound_student

logging.basicConfig(level=logging.INFO)
router = Router()


# === Клавиатура ===

def build_main_keyboard():
    buttons = [
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Завтра")],
        [KeyboardButton(text="🗓 Неделя"), KeyboardButton(text="📋 Предметы")],
        [KeyboardButton(text="👨‍🏫 Преподаватель"), KeyboardButton(text="👥 Сменить группу")],
    ]
    bottom_row = []
    if AD_FULL_TEXT:
        bottom_row.append(KeyboardButton(text=AD_BUTTON_LABEL))
    buttons.append(bottom_row)
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

MAIN_KEYBOARD = build_main_keyboard()


# === Утилиты ===

def subject_hash(subject: str) -> str:
    return hashlib.md5(subject.encode()).hexdigest()[:10]


def normalize_group_query(text: str) -> str:
    text = text.strip().lower()
    if text.startswith('c') and len(text) > 1 and text[1:2].isdigit():
        text = 'с' + text[1:]
    if text.startswith('pp'):
        text = 'пп' + text[2:]
    return text


def detect_course(code: str) -> int:
    m = re.search(r'\d', code)
    if not m:
        return 0
    first_digit = int(m.group())
    if code.lower().startswith('мг') or code.lower().startswith('mg'):
        return first_digit - 4
    return first_digit


def filter_lessons(lessons: list, user_subjects: list[str]) -> list:
    if not user_subjects:
        return list(lessons)
    return [l for l in lessons if l['subject'] in user_subjects]


def get_schedule_for_date(group_id: int, d: date, chat_id: int) -> list:
    conn = get_connection()
    lessons = get_lessons_for_date(conn, group_id, d.strftime('%Y-%m-%d'))
    user_subj = get_user_subjects(conn, chat_id, group_id)
    conn.close()
    return filter_lessons(lessons, user_subj)


def get_week_days(group_id: int, monday: date, chat_id: int) -> dict:
    sunday = monday + timedelta(days=6)
    conn = get_connection()
    all_lessons = get_lessons_for_week(
        conn, group_id, monday.strftime('%Y-%m-%d'), sunday.strftime('%Y-%m-%d'),
    )
    user_subj = get_user_subjects(conn, chat_id, group_id)
    conn.close()

    filtered = filter_lessons(all_lessons, user_subj)
    days = defaultdict(list)
    for l in filtered:
        d = datetime.strptime(l['date'], '%Y-%m-%d').date()
        days[d].append(l)

    for i in range(6):  # Пн-Сб
        d = monday + timedelta(days=i)
        if d not in days:
            days[d] = []

    return dict(days)


# === Трекинг ===

def do_track(message: Message, action: str, detail: str = None):
    """Сохранить данные пользователя и записать действие."""
    user = message.from_user
    conn = get_connection()
    # данные о группе
    ug = get_user_group(conn, message.chat.id)
    group_code = ug['group_code'] if ug else None
    track_user(conn, message.chat.id,
               username=user.username,
               first_name=user.first_name,
               last_name=user.last_name,
               group_code=group_code)
    log_action(conn, message.chat.id, action, detail)
    conn.close()


def do_track_cb(callback: CallbackQuery, action: str, detail: str = None):
    """Трекинг для callback query."""
    user = callback.from_user
    conn = get_connection()
    ug = get_user_group(conn, callback.message.chat.id)
    group_code = ug['group_code'] if ug else None
    track_user(conn, callback.message.chat.id,
               username=user.username,
               first_name=user.first_name,
               last_name=user.last_name,
               group_code=group_code)
    log_action(conn, callback.message.chat.id, action, detail)
    conn.close()


# === Навигация по неделям ===

def week_nav_keyboard(monday: date, group_id: int) -> InlineKeyboardMarkup:
    conn = get_connection()
    min_d, max_d = get_date_range(conn, group_id)
    conn.close()

    buttons = []
    prev_monday = monday - timedelta(days=7)
    next_monday = monday + timedelta(days=7)

    prev_btn = None
    next_btn = None

    if min_d:
        min_date = datetime.strptime(min_d, '%Y-%m-%d').date()
        max_date = datetime.strptime(max_d, '%Y-%m-%d').date()
        if prev_monday >= min_date - timedelta(days=7):
            prev_btn = InlineKeyboardButton(
                text="← Пред. неделя", callback_data=f"week:{prev_monday.isoformat()}")
        if next_monday <= max_date + timedelta(days=7):
            next_btn = InlineKeyboardButton(
                text="След. неделя →", callback_data=f"week:{next_monday.isoformat()}")
    else:
        prev_btn = InlineKeyboardButton(
            text="← Пред. неделя", callback_data=f"week:{prev_monday.isoformat()}")
        next_btn = InlineKeyboardButton(
            text="След. неделя →", callback_data=f"week:{next_monday.isoformat()}")

    row = []
    if prev_btn:
        row.append(prev_btn)
    if next_btn:
        row.append(next_btn)

    return InlineKeyboardMarkup(inline_keyboard=[row] if row else [])


def day_nav_keyboard(d: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="← Вчера", callback_data=f"day:{(d - timedelta(days=1)).isoformat()}"),
        InlineKeyboardButton(text="Завтра →", callback_data=f"day:{(d + timedelta(days=1)).isoformat()}"),
    ]])


# === Проверка группы ===

async def check_group(message_or_callback) -> dict | None:
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
            "⚠️ Сначала выбери группу!\nНажми <b>👥 Сменить группу</b> или напиши номер.",
            parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)
        return None
    return user


# === /start ===

@router.message(CommandStart())
async def cmd_start(message: Message):
    do_track(message, 'start')

    conn = get_connection()
    user = get_user_group(conn, message.chat.id)
    conn.close()

    if user:
        await message.answer(
            f"👋 С возвращением! Твоя группа: <b>{user['group_code']}</b>\n\n"
            f"Используй кнопки внизу 👇",
            parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)
    else:
        await message.answer(
            "👋 Привет! Я бот расписания МГУ.\n\nДля начала выбери свою группу:",
            parse_mode=ParseMode.HTML)
        await show_department_selection(message)


# === Выбор группы кнопками ===

async def show_department_selection(message: Message):
    conn = get_connection()
    rows = conn.execute(
        """SELECT DISTINCT g.department, g.program FROM groups_ g
           JOIN faculties f ON g.faculty_id = f.id
           WHERE g.department != '' AND g.program != ''
           ORDER BY g.department, g.program"""
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer("База пуста. Запусти парсер: <code>python run_parser.py socio</code>",
                             parse_mode=ParseMode.HTML)
        return

    buttons = []
    seen = set()
    for r in rows:
        key = f"{r['department']}|{r['program']}"
        label = f"{r['department']} — {r['program']}" if r['program'] else r['department']
        if key not in seen:
            seen.add(key)
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"dept:{key}")])

    buttons.append([InlineKeyboardButton(text="🔍 Найти себя по фамилии", callback_data="find_by_name")])
    buttons.append([InlineKeyboardButton(text="✏️ Ввести номер группы", callback_data="manual_input")])
    await message.answer("📚 Выбери направление:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == 'manual_input')
async def on_manual_input(callback: CallbackQuery):
    await callback.message.edit_text(
        "Напиши номер группы, например:\n<b>с403</b>, <b>403</b>, <b>пп201</b>, <b>мг52МКПП</b>",
        parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == 'find_by_name')
async def on_find_by_name(callback: CallbackQuery):
    await callback.message.edit_text(
        "🔍 Напиши свою фамилию (или первые буквы):",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith('bind:'))
async def on_bind_student(callback: CallbackQuery):
    student_id = int(callback.data.split(':')[1])

    conn = get_connection()
    student = conn.execute(
        """SELECT s.*, g.code as group_code FROM students s
           JOIN groups_ g ON s.group_id = g.id WHERE s.id = ?""",
        (student_id,)
    ).fetchone()

    if not student:
        await callback.answer("Студент не найден")
        conn.close()
        return

    bind_student(conn, callback.message.chat.id, student_id)
    set_user_group(conn, callback.message.chat.id, student['group_id'])

    # Проверить предметы по выбору
    conflicts = get_conflicting_subjects(conn, student['group_id'])
    conn.close()

    do_track_cb(callback, 'bind_student', f"{student['full_name']} ({student['group_code']})")

    text = (
        f"✅ <b>{student['full_name']}</b>\n"
        f"   Группа: {student['group_code']}\n"
    )
    if conflicts:
        text += (
            f"\n⚠️ В расписании <b>{len(conflicts)}</b> предметов по выбору.\n"
            f"Нажми <b>📋 Предметы</b>, чтобы отметить свои.\n"
        )
    text += "\nИспользуй кнопки внизу 👇"

    await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
    await callback.message.answer("Готово!", reply_markup=MAIN_KEYBOARD)
    await callback.answer()

@router.callback_query(F.data == 'back_to_dept')
async def on_back_to_dept(callback: CallbackQuery):
    conn = get_connection()
    rows = conn.execute(
        """SELECT DISTINCT g.department, g.program FROM groups_ g
           WHERE g.department != '' AND g.program != '' ORDER BY g.department, g.program"""
    ).fetchall()
    conn.close()

    buttons = []
    seen = set()
    for r in rows:
        key = f"{r['department']}|{r['program']}"
        label = f"{r['department']} — {r['program']}" if r['program'] else r['department']
        if key not in seen:
            seen.add(key)
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"dept:{key}")])
    buttons.append([InlineKeyboardButton(text="🔍 Найти себя по фамилии", callback_data="find_by_name")])
    buttons.append([InlineKeyboardButton(text="✏️ Ввести номер группы", callback_data="manual_input")])

    await callback.message.edit_text("📚 Выбери направление:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith('dept:'))
async def on_department_select(callback: CallbackQuery):
    parts = callback.data.split(':', 1)[1].split('|')
    department = parts[0]
    program = parts[1] if len(parts) > 1 else ''

    conn = get_connection()
    groups = conn.execute(
        "SELECT id, code FROM groups_ WHERE department = ? AND program = ? ORDER BY code",
        (department, program)
    ).fetchall()
    conn.close()

    courses = {}
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
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"course:{department}|{program}|{c}")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="back_to_dept")])

    await callback.message.edit_text(
        f"📚 {department} — {program}\nВыбери курс:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith('course:'))
async def on_course_select(callback: CallbackQuery):
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
        buttons.append([InlineKeyboardButton(text=r['code'], callback_data=f"grp:{r['id']}")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data=f"dept:{department}|{program}")])

    await callback.message.edit_text(
        f"📚 {department} — {program} — {target_course} курс\nВыбери группу:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith('grp:'))
async def on_group_select(callback: CallbackQuery):
    group_id = int(callback.data.split(':')[1])
    conn = get_connection()
    set_user_group(conn, callback.message.chat.id, group_id)
    user = get_user_group(conn, callback.message.chat.id)
    conflicts = get_conflicting_subjects(conn, user['group_id'])
    conn.close()

    do_track_cb(callback, 'set_group', user['group_code'])

    text = (
        f"✅ Группа: <b>{user['group_code']}</b>\n"
        f"   {user['faculty_name']}, {user['department']}\n"
    )
    if conflicts:
        text += (
            f"\n⚠️ В расписании <b>{len(conflicts)}</b> предметов по выбору.\n"
            f"Нажми <b>📋 Предметы</b>, чтобы отметить свои.\n"
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
    do_track(message, 'today')

    d = date.today()
    lessons = get_schedule_for_date(user['group_id'], d, message.chat.id)

    text = f"👥 <b>{user['group_code']}</b>\n\n"
    text += format_day_schedule(lessons, d)

    if d.weekday() >= 5 and not lessons:
        text += "\n\nНажми <b>🗓 Неделя</b> — покажу следующую."

    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)


@router.message(F.text == "📆 Завтра")
@router.message(Command('завтра', 'tomorrow'))
async def cmd_tomorrow(message: Message):
    user = await check_group(message)
    if not user:
        return
    do_track(message, 'tomorrow')

    d = date.today() + timedelta(days=1)
    lessons = get_schedule_for_date(user['group_id'], d, message.chat.id)

    text = f"👥 <b>{user['group_code']}</b>\n\n"
    text += format_day_schedule(lessons, d)

    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)


@router.message(F.text == "🗓 Неделя")
@router.message(Command('неделя', 'week'))
async def cmd_week(message: Message):
    user = await check_group(message)
    if not user:
        return
    do_track(message, 'week')

    today = date.today()
    monday = today - timedelta(days=today.weekday())
    if today.weekday() >= 5:
        monday = monday + timedelta(days=7)

    await send_week(message, user, monday)


async def send_week(message_or_callback, user: dict, monday: date):
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
    keyboard = week_nav_keyboard(monday, user['group_id'])

    if len(text) > 4000:
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.message.edit_text(
                header + "⬇️ Расписание по дням:", parse_mode=ParseMode.HTML)
            send = message_or_callback.message.answer
        else:
            await message_or_callback.answer(
                header + "⬇️ Расписание по дням:",
                parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)
            send = message_or_callback.answer

        for d in sorted(days.keys()):
            if d.weekday() < 6:
                await send(format_day_schedule(days[d], d), parse_mode=ParseMode.HTML)
        await send("Навигация:", reply_markup=keyboard)
    else:
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.message.edit_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        else:
            await message_or_callback.answer(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


@router.callback_query(F.data.startswith('week:'))
async def on_week_navigate(callback: CallbackQuery):
    user = await check_group(callback)
    if not user:
        return
    date_str = callback.data.split(':')[1]
    monday = datetime.strptime(date_str, '%Y-%m-%d').date()
    await send_week(callback, user, monday)
    await callback.answer()


@router.callback_query(F.data.startswith('day:'))
async def on_day_navigate(callback: CallbackQuery):
    user = await check_group(callback)
    if not user:
        return
    date_str = callback.data.split(':')[1]
    d = datetime.strptime(date_str, '%Y-%m-%d').date()
    lessons = get_schedule_for_date(user['group_id'], d, callback.message.chat.id)

    text = f"👥 <b>{user['group_code']}</b>\n\n"
    text += format_day_schedule(lessons, d)

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML, reply_markup=day_nav_keyboard(d))
    await callback.answer()


# === Предметы по выбору ===

@router.message(F.text == "📋 Предметы")
@router.message(Command('предметы', 'subjects'))
async def cmd_subjects(message: Message):
    user = await check_group(message)
    if not user:
        return
    do_track(message, 'subjects')

    conn = get_connection()
    conflicts = get_conflicting_subjects(conn, user['group_id'])
    selected = get_user_subjects(conn, message.chat.id, user['group_id'])
    conn.close()

    if not conflicts:
        await message.answer("✅ В расписании нет предметов по выбору!", reply_markup=MAIN_KEYBOARD)
        return

    buttons = []
    for s in conflicts:
        check = '✅' if s['subject'] in selected else '⬜️'
        name = format_subject_button(s)
        buttons.append([InlineKeyboardButton(
            text=f"{check} {name}", callback_data=f"subj:{subject_hash(s['subject'])}")])
    buttons.append([
        InlineKeyboardButton(text="🔄 Сбросить всё", callback_data="subj:reset"),
        InlineKeyboardButton(text="✅ Готово", callback_data="subj:done"),
    ])

    await message.answer(
        f"📋 <b>Предметы по выбору</b> ({user['group_code']})\n\n"
        f"Выбрано: {len(selected)} из {len(conflicts)}\n\n"
        f"Нажми на предмет чтобы добавить/убрать.\n"
        f"В расписании будут только отмеченные ✅\n"
        f"Если ничего не выбрано — показывается всё.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith('subj:'))
async def on_subject_toggle(callback: CallbackQuery):
    data = callback.data.split(':', 1)[1]

    if data == 'done':
        conn = get_connection()
        user = get_user_group(conn, callback.message.chat.id)
        selected = get_user_subjects(conn, callback.message.chat.id, user['group_id']) if user else []
        conn.close()
        count = len(selected)
        text = (f"✅ Сохранено! Выбрано предметов: {count}\nВ расписании будут только они."
                if count > 0 else "✅ Фильтр сброшен — будут показаны все предметы.")
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
        conn.execute("DELETE FROM user_subjects WHERE chat_id = ? AND group_id = ?",
                     (callback.message.chat.id, user['group_id']))
        conn.commit()
        await callback.answer("Фильтр сброшен!")
    else:
        conflicts = get_conflicting_subjects(conn, user['group_id'])
        target = next((s for s in conflicts if subject_hash(s['subject']) == data), None)
        if not target:
            await callback.answer("Предмет не найден")
            conn.close()
            return
        is_selected = toggle_user_subject(
            conn, callback.message.chat.id, user['group_id'], target['subject'])
        await callback.answer(f"Предмет {'добавлен ✅' if is_selected else 'убран ⬜️'}")

    # обновить кнопки
    conflicts = get_conflicting_subjects(conn, user['group_id'])
    selected = get_user_subjects(conn, callback.message.chat.id, user['group_id'])
    conn.close()

    buttons = []
    for s in conflicts:
        check = '✅' if s['subject'] in selected else '⬜️'
        name = format_subject_button(s)
        buttons.append([
            InlineKeyboardButton(
                text=f"{check} {name}", callback_data=f"subj:{subject_hash(s['subject'])}")])
    buttons.append([
        InlineKeyboardButton(text="🔄 Сбросить всё", callback_data="subj:reset"),
        InlineKeyboardButton(text="✅ Готово", callback_data="subj:done"),
    ])
    await callback.message.edit_reply_markup(
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# === Реклама / Полезное ===

@router.message(F.text == AD_BUTTON_LABEL)
async def cmd_ad(message: Message):
    if not AD_FULL_TEXT:
        return
    do_track(message, 'ad_click')
    await message.answer(AD_FULL_TEXT, parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)


# === Статистика (только для админа) ===

@router.message(Command('stats'))
async def cmd_stats(message: Message):
    if str(message.chat.id) != str(ADMIN_CHAT_ID):
        return

    conn = get_connection()
    s = get_stats(conn)
    conn.close()

    top = '\n'.join(f"  {code} — {cnt} чел." for code, cnt in s['top_groups'][:10])

    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"Всего пользователей: {s['total']}\n"
        f"Активных за 7 дней: {s['active_7d']}\n"
        f"Действий сегодня: {s['today_actions']}\n"
        f"Клики по рекламе: {s['ad_clicks']}\n\n"
        f"<b>Топ групп:</b>\n{top or '  нет данных'}",
        parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)

# === Расписание преподавателя ===

@router.message(F.text == "👨‍🏫 Преподаватель")
async def cmd_teacher_start(message: Message):
    do_track(message, 'teacher_search')
    await message.answer(
        "👨‍🏫 Напиши фамилию преподавателя (или первые буквы):",
        parse_mode=ParseMode.HTML,
        reply_markup=MAIN_KEYBOARD,
    )


@router.callback_query(F.data.startswith('tch:'))
async def on_teacher_select(callback: CallbackQuery):
    teacher_name = callback.data.split(':', 1)[1]

    conn = get_connection()
    rows = conn.execute(
        """SELECT date, pair_number, time_start, time_end, subject, subject_abbr,
                  lesson_type, room, teacher, g.code as group_code
           FROM lessons l
           JOIN groups_ g ON l.group_id = g.id
           WHERE l.teacher LIKE ? AND l.date >= date('now') AND l.date <= date('now', '+14 days')
           ORDER BY l.date, l.pair_number""",
        (f'%{teacher_name}%',)
    ).fetchall()
    conn.close()

    if not rows:
        await callback.message.edit_text(
            f"👨‍🏫 <b>{teacher_name}</b>\n\nНет занятий в ближайшие 2 недели.",
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
        return

    # Группируем по дням
    from collections import defaultdict
    from datetime import datetime
    days = defaultdict(list)
    for r in rows:
        d = datetime.strptime(r['date'], '%Y-%m-%d').date()
        days[d].append(r)

    text = f"👨‍🏫 <b>{teacher_name}</b>\n"
    for d in sorted(days.keys()):
        from bot.formatting import format_date_header, TYPE_EMOJI
        text += f"\n{format_date_header(d)}\n"

        # Группируем: (pair, subject, room, type) → [группы]
        seen = {}
        order = []
        for l in days[d]:
            key = (l['pair_number'], l['subject'], l['room'], l['lesson_type'],
                   l['time_start'], l['time_end'], l['subject_abbr'])
            if key not in seen:
                seen[key] = []
                order.append(key)
            seen[key].append(l['group_code'])

        for key in order:
            pair, subj, room, ltype, t_start, t_end, abbr = key
            groups = seen[key]
            emoji = TYPE_EMOJI.get(ltype, '📌')
            name = abbr or subj
            if len(name) > 20:
                name = name[:17] + '...'
            groups_str = ', '.join(groups)
            text += f"  {emoji} {pair} ({t_start}–{t_end}) {name} {room} [{ltype}]\n"
            text += f"     гр. {groups_str}\n"

    if len(text) > 4000:
        text = text[:3950] + "\n\n..."

    await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
    await callback.answer()
    
# === Сменить группу / Помощь ===

@router.message(F.text == "👥 Сменить группу")
@router.message(Command('группа', 'group'))
async def cmd_change_group(message: Message):
    do_track(message, 'change_group')
    await show_department_selection(message)


@router.message(Command('помощь', 'help'))
async def cmd_help(message: Message):
    do_track(message, 'help')
    await message.answer(
        "📌 <b>Как пользоваться:</b>\n\n"
        "Используй кнопки внизу экрана.\n\n"
        "📅 Сегодня — расписание на сегодня\n"
        "📆 Завтра — на завтра\n"
        "🗓 Неделя — на неделю (с навигацией)\n"
        "📋 Предметы — выбрать свои предметы\n"
        "👥 Сменить группу — выбрать другую группу\n\n"
        "Также можно написать номер группы: <b>403</b>, <b>с403</b>",
        parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)


# === Текстовый поиск группы ===

@router.message(F.text & ~F.text.startswith('/'))
async def on_text_message(message: Message):
    text = message.text.strip()

    if text in ("📅 Сегодня", "📆 Завтра", "🗓 Неделя",
                "📋 Предметы", "👥 Сменить группу", "👨‍🏫 Преподаватель", AD_BUTTON_LABEL):
        return
# Поиск преподавателя — если текст похож на фамилию (начинается с заглавной, нет цифр)
    if len(text) >= 2 and text[0].isupper() and not any(c.isdigit() for c in text):
        conn = get_connection()
        rows = conn.execute(
            """SELECT DISTINCT teacher FROM lessons
               WHERE teacher LIKE ? AND teacher != ''
               ORDER BY teacher LIMIT 50""",
            (f'%{text}%',)
        ).fetchall()
        conn.close()

        if rows:
            # Разбиваем "Осипова Н.Г., Елишев С.О." на отдельных
            seen = set()
            individual = []
            for r in rows:
                for name in r['teacher'].split(', '):
                    name = name.strip()
                    if name and text.lower() in name.lower() and name not in seen:
                        seen.add(name)
                        individual.append(name)

            individual.sort()
            buttons = [[InlineKeyboardButton(
                text=name, callback_data=f"tch:{name}"
            )] for name in individual[:10]]

            await message.answer(
                f"👨‍🏫 Найдено преподавателей: {len(individual)}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            return
            # Поиск студента — если текст с заглавной, >= 3 букв, нет цифр
    if len(text) >= 3 and text[0].isupper() and not any(c.isdigit() for c in text):
        conn = get_connection()
        found = get_students_by_name(conn, text)
        conn.close()

        if found:
            buttons = [[InlineKeyboardButton(
                text=f"{s['full_name']} ({s['group_code']})",
                callback_data=f"bind:{s['id']}",
            )] for s in found]

            await message.answer(
                f"👤 Найдено: {len(found)}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            )
            return
            
    query = normalize_group_query(text)
    query_with_prefix = 'с' + query if query.isdigit() else query

    conn = get_connection()
    rows = conn.execute(
        """SELECT g.id, g.code, g.department, g.program, f.name as faculty_name
           FROM groups_ g JOIN faculties f ON g.faculty_id = f.id
           WHERE LOWER(g.code) LIKE ? OR LOWER(g.code) LIKE ?
           ORDER BY g.code LIMIT 20""",
        (f'%{query}%', f'%{query_with_prefix}%')
    ).fetchall()
    conn.close()

    if not rows:
        await message.answer(
            f"🔍 Группа «{text}» не найдена.\nПопробуй <b>👥 Сменить группу</b>.",
            parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)
        return

    if len(rows) == 1:
        conn = get_connection()
        set_user_group(conn, message.chat.id, rows[0]['id'])
        user = get_user_group(conn, message.chat.id)
        conn.close()
        do_track(message, 'set_group', user['group_code'])
        await message.answer(
            f"✅ Группа: <b>{user['group_code']}</b>\n   {user['faculty_name']}, {user['department']}",
            parse_mode=ParseMode.HTML, reply_markup=MAIN_KEYBOARD)
        return

    buttons = [[InlineKeyboardButton(
        text=f"{g['code']} ({g['department']}, {g['program']})",
        callback_data=f"grp:{g['id']}")] for g in rows]
    await message.answer(f"🔍 Найдено {len(rows)} групп:",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# === Запуск ===

async def main():
    if not BOT_TOKEN or BOT_TOKEN == 'your_telegram_bot_token_here':
        print("[bot] BOT_TOKEN not set in .env")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    print("[bot] Started. Press Ctrl+C to stop.")
    await dp.start_polling(bot)


if __name__ == '__main__':
    asyncio.run(main())
