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
    Message, CallbackQuery, ErrorEvent,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from core.config import BOT_TOKEN, ADMIN_CHAT_ID, AD_FULL_TEXT, AD_BUTTON_LABEL
from core.database import (
    get_connection, get_user_group, set_user_group,
    get_lessons_for_date, get_lessons_for_week, get_date_range,
    get_conflicting_subjects, get_user_subjects, toggle_user_subject,
    track_user, log_action, get_stats,
    get_stream_subjects, get_stream_variants,
    set_user_stream, clear_user_stream, resolve_user_stream,
)
from bot.formatting import (
    format_day_schedule, format_week_schedule, format_subject_button, format_slots,
)
from core.alerts import send_admin_alert
from core.database import find_groups_by_code
from core.db_students import (
    get_students_by_name, find_teachers_by_name,
    bind_student, get_bound_student, apply_student_filter,
)

logging.basicConfig(level=logging.INFO)
router = Router()


class Search(StatesGroup):
    """
    Что именно бот сейчас ждёт текстом.

    Раньше состояния не было вовсе, и все три кнопки — «найди себя по
    фамилии», «напиши фамилию преподавателя», «введи номер группы» — вели
    в один обработчик, который угадывал намерение по заглавной букве.
    Из-за этого поиск преподавателя перехватывался поиском студента:
    27% преподавателей были недостижимы, потому что находился однофамилец.
    """
    group = State()      # ждём номер группы
    student = State()    # ждём фамилию студента
    teacher = State()    # ждём фамилию преподавателя


# === Клавиатура ===

LANG_BUTTON = "🔤 Мой язык"
HELP_BUTTON = "❓ Помощь"


def build_main_keyboard(with_language: bool = False):
    """
    Раскладка по смыслу рядов, а не по остаточному принципу:
      1. расписание
      2. что настраивается под себя, и помощь рядом с этим
      3. поиск и смена группы
      4. реклама, если задана
    """
    personal = [KeyboardButton(text="📋 Предметы")]
    if with_language:
        personal.append(KeyboardButton(text=LANG_BUTTON))
    personal.append(KeyboardButton(text=HELP_BUTTON))

    buttons = [
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Завтра"),
         KeyboardButton(text="🗓 Неделя")],
        personal,
        [KeyboardButton(text="👨‍🏫 Преподаватель"), KeyboardButton(text="👥 Сменить группу")],
    ]
    if AD_FULL_TEXT:
        buttons.append([KeyboardButton(text=AD_BUTTON_LABEL)])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


MAIN_KEYBOARD = build_main_keyboard()
LANG_KEYBOARD = build_main_keyboard(with_language=True)

# Тексты кнопок нижней клавиатуры: их не надо принимать за поисковый запрос
MAIN_BUTTON_TEXTS = {
    "📅 Сегодня", "📆 Завтра", "🗓 Неделя",
    "📋 Предметы", "👥 Сменить группу", "👨‍🏫 Преподаватель",
    LANG_BUTTON, HELP_BUTTON,
}
if AD_BUTTON_LABEL:
    MAIN_BUTTON_TEXTS.add(AD_BUTTON_LABEL)


def keyboard_for(chat_id: int):
    """
    Клавиатура под конкретного человека.

    Кнопка языка показывается только тем, у чьей группы есть языковые
    потоки — это девять групп из сорока девяти. Остальным она была бы
    кнопкой, которая всегда отвечает «у тебя такого нет».
    """
    if not chat_id:
        return MAIN_KEYBOARD
    try:
        conn = get_connection()
        row = conn.execute(
            """SELECT 1 FROM subscriptions s
                 JOIN lessons l ON l.group_id = s.group_id
                WHERE s.chat_id = ? AND l.subgroup != '' AND l.date >= date('now')
                LIMIT 1""", (chat_id,)).fetchone()
        conn.close()
        return LANG_KEYBOARD if row else MAIN_KEYBOARD
    except Exception:
        return MAIN_KEYBOARD


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
    """
    Оставить только выбранные предметы.

    Языковые потоки фильтр не трогает: человек их не выбирал, они приходят
    отдельным блоком «не у всех», и вырезать их по названию предмета
    означало бы просто спрятать.
    """
    if not user_subjects:
        return list(lessons)
    from bot.formatting import field
    return [l for l in lessons
            if field(l, 'subgroup') or l['subject'] in user_subjects]


def get_schedule_for_date(group_id: int, d: date, chat_id: int) -> tuple:
    """
    (занятия, диапазон дат группы).

    Диапазон нужен, чтобы отличить «в этот день пар нет» от «расписания
    на этот день ещё не выложили» — снаружи это выглядело одинаково.
    """
    conn = get_connection()
    lessons = get_lessons_for_date(conn, group_id, d.strftime('%Y-%m-%d'))
    user_subj = get_user_subjects(conn, chat_id, group_id)
    data_range = get_date_range(conn, group_id)
    stream_choice = resolve_user_stream(conn, chat_id, group_id)
    conn.close()
    return filter_lessons(lessons, user_subj), data_range, stream_choice


def get_week_days(group_id: int, monday: date, chat_id: int) -> tuple:
    """(дни недели -> занятия, диапазон дат группы)."""
    sunday = monday + timedelta(days=6)
    conn = get_connection()
    all_lessons = get_lessons_for_week(
        conn, group_id, monday.strftime('%Y-%m-%d'), sunday.strftime('%Y-%m-%d'),
    )
    user_subj = get_user_subjects(conn, chat_id, group_id)
    data_range = get_date_range(conn, group_id)
    stream_choice = resolve_user_stream(conn, chat_id, group_id)
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

    return dict(days), data_range, stream_choice


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

# Сколько группа может не встречаться в навигации сайта, прежде чем считать
# её пропавшей. Обход идёт трижды в сутки, так что неделя — это два десятка
# прогонов подряд. Запас нужен: сайт регулярно лежит, и хотя required=True
# превращает это в FetchError, а не в пустой успех, перестраховка дешевле
# чем сказать «группы нет» тому, у кого она есть.
GROUP_GONE_AFTER = timedelta(days=7)


def group_gone_note(user: dict) -> str:
    """
    Предупреждение для группы, исчезнувшей с сайта. Пусто, если всё в порядке.

    Наборы выпускаются, коды пропадают из навигации, а подписка в базе
    остаётся навсегда: подписчик пп402 каждый день видел бодрое
    «🎉 Нет занятий!» и никакого объяснения (docs/TODO.md §1).

    last_seen пустой — это база, собранная до появления колонки. Молчим:
    сказать «группы нет» тому, у кого она есть, хуже, чем не сказать ничего.
    """
    seen = user.get('last_seen')
    if not seen:
        return ''
    if date.today() - date.fromisoformat(seen) <= GROUP_GONE_AFTER:
        return ''
    return ("⚠️ Этой группы больше нет в расписании факультета — похоже, "
            "набор выпустился.\n"
            "Нажмите <b>👥 Сменить группу</b>, чтобы выбрать другую.\n\n")


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
            parse_mode=ParseMode.HTML, reply_markup=keyboard_for(chat_id))
        return None
    return user


# === /start ===

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    do_track(message, 'start')

    conn = get_connection()
    user = get_user_group(conn, message.chat.id)
    conn.close()

    if user:
        await message.answer(
            f"👋 С возвращением! Твоя группа: <b>{user['group_code']}</b>\n\n"
            f"Используй кнопки внизу 👇",
            parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))
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
async def on_manual_input(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Search.group)
    await callback.message.edit_text(
        "Напиши номер группы, например:\n<b>с403</b>, <b>403</b>, <b>пп201</b>, <b>мг52МКПП</b>",
        parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data == 'find_by_name')
async def on_find_by_name(callback: CallbackQuery, state: FSMContext):
    await state.set_state(Search.student)
    await callback.message.edit_text(
        "🔍 Напиши свою фамилию (минимум 2 буквы):",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.callback_query(F.data.startswith('bind:'))
async def on_bind_student(callback: CallbackQuery, state: FSMContext):
    await state.clear()
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
    # Автоматически установить фильтр предметов
    conn2 = get_connection()
    applied = apply_student_filter(conn2, callback.message.chat.id, student_id, student['group_id'])
    conn2.close()
    do_track_cb(callback, 'bind_student', f"{student['full_name']} ({student['group_code']})")

    text = (
        f"✅ <b>{student['full_name']}</b>\n"
        f"   Группа: {student['group_code']}\n"
    )
    if applied:
        text += f"\n✅ Автоматически отмечено <b>{applied}</b> твоих предметов по выбору."
    elif conflicts:
        text += (
            f"\n⚠️ В расписании <b>{len(conflicts)}</b> предметов по выбору.\n"
            f"Нажми <b>📋 Предметы</b>, чтобы отметить свои.\n"
        )
    text += "\nИспользуй кнопки внизу 👇"

    await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
    await callback.message.answer("Готово!", reply_markup=keyboard_for(callback.message.chat.id))
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
async def on_group_select(callback: CallbackQuery, state: FSMContext):
    await state.clear()
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
    await callback.message.answer("Готово!", reply_markup=keyboard_for(callback.message.chat.id))
    await callback.answer()


# === Расписание ===

@router.message(F.text == "📅 Сегодня")
@router.message(Command('сегодня', 'today'))
async def cmd_today(message: Message, state: FSMContext):
    await state.clear()
    user = await check_group(message)
    if not user:
        return
    do_track(message, 'today')

    d = date.today()
    lessons, data_range, stream_choice = get_schedule_for_date(
        user['group_id'], d, message.chat.id)

    text = f"👥 <b>{user['group_code']}</b>\n\n" + group_gone_note(user)
    text += format_day_schedule(lessons, d, data_range=data_range,
                                stream_choice=stream_choice)

    if d.weekday() >= 5 and not lessons:
        text += "\n\nНажми <b>🗓 Неделя</b> — покажу следующую."

    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))


@router.message(F.text == "📆 Завтра")
@router.message(Command('завтра', 'tomorrow'))
async def cmd_tomorrow(message: Message, state: FSMContext):
    await state.clear()
    user = await check_group(message)
    if not user:
        return
    do_track(message, 'tomorrow')

    d = date.today() + timedelta(days=1)
    lessons, data_range, stream_choice = get_schedule_for_date(
        user['group_id'], d, message.chat.id)

    text = f"👥 <b>{user['group_code']}</b>\n\n" + group_gone_note(user)
    text += format_day_schedule(lessons, d, data_range=data_range,
                                stream_choice=stream_choice)

    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))


@router.message(F.text == "🗓 Неделя")
@router.message(Command('неделя', 'week'))
async def cmd_week(message: Message, state: FSMContext):
    await state.clear()
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

    days, data_range, stream_choice = get_week_days(user['group_id'], monday, chat_id)
    sunday = monday + timedelta(days=6)

    header = (
        f"👥 <b>{user['group_code']}</b>\n"
        f"📅 Неделя: {monday.strftime('%d.%m')} — {sunday.strftime('%d.%m')}\n\n"
        + group_gone_note(user)
    )

    text = header + format_week_schedule(days, data_range, stream_choice)
    keyboard = week_nav_keyboard(monday, user['group_id'])

    if len(text) > 4000:
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.message.edit_text(
                header + "⬇️ Расписание по дням:", parse_mode=ParseMode.HTML)
            send = message_or_callback.message.answer
        else:
            await message_or_callback.answer(
                header + "⬇️ Расписание по дням:",
                parse_mode=ParseMode.HTML, reply_markup=keyboard_for(chat_id))
            send = message_or_callback.answer

        for d in sorted(days.keys()):
            if d.weekday() < 6:
                await send(format_day_schedule(days[d], d, data_range=data_range,
                                               stream_choice=stream_choice),
                           parse_mode=ParseMode.HTML)
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
    lessons, data_range, stream_choice = get_schedule_for_date(
        user['group_id'], d, callback.message.chat.id)

    text = f"👥 <b>{user['group_code']}</b>\n\n" + group_gone_note(user)
    text += format_day_schedule(lessons, d, data_range=data_range,
                                stream_choice=stream_choice)

    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML, reply_markup=day_nav_keyboard(d))
    await callback.answer()


# === Предметы по выбору ===

@router.message(F.text == "📋 Предметы")
@router.message(Command('предметы', 'subjects'))
async def cmd_subjects(message: Message, state: FSMContext):
    await state.clear()
    user = await check_group(message)
    if not user:
        return
    do_track(message, 'subjects')

    conn = get_connection()
    conflicts = get_conflicting_subjects(conn, user['group_id'])
    selected = get_user_subjects(conn, message.chat.id, user['group_id'])
    conn.close()

    if not conflicts:
        await message.answer("✅ В расписании нет предметов по выбору!", reply_markup=keyboard_for(message.chat.id))
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


# === Выбор языкового потока ===
#
# Выбор хранится смыслом — предмет и преподаватель, — а не номером потока.
# Номер «с101-3» живёт один семестр: коды групп на сайте пересобираются,
# да и студент переходит на курс, где языков уже нет. Проверку на
# актуальность делает resolve_user_stream: если выбранного языка у группы
# больше нет, фильтр молча выключается и показываются все потоки.


@router.message(F.text == LANG_BUTTON)
@router.message(Command('язык', 'language', 'lang'))
async def cmd_language(message: Message, state: FSMContext):
    await state.clear()
    user = await check_group(message)
    if not user:
        return
    do_track(message, 'language')

    conn = get_connection()
    subjects = get_stream_subjects(conn, user['group_id'])
    current = resolve_user_stream(conn, message.chat.id, user['group_id'])
    conn.close()

    if not subjects:
        await message.answer(
            "🔤 У твоей группы нет языковых потоков.\n"
            "Языки идут только на первом курсе.",
            parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))
        return

    buttons = [[InlineKeyboardButton(
        text=f"{'✅ ' if current and current['subject'] == subj else ''}{subj}",
        callback_data=f"lang:s:{subject_hash(subj)}")] for subj, _ in subjects]
    buttons.append([InlineKeyboardButton(text="Показывать все", callback_data="lang:all")])

    if current:
        now = current['subject']
        if current['teacher']:
            now += f" — {current['teacher']}"
        head = f"🔤 Сейчас выбрано: <b>{now}</b>\n\nВыбери язык:"
    else:
        head = ("🔤 <b>Языковые потоки</b>\n\n"
                "Группа учит разные языки, и у каждого потока своё время "
                "и преподаватель. Выбери свой — в расписании останется только он.\n\n"
                "Какой язык:")

    await message.answer(head, parse_mode=ParseMode.HTML,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data == 'lang:all')
async def on_lang_all(callback: CallbackQuery):
    user = await check_group(callback)
    if not user:
        return
    conn = get_connection()
    clear_user_stream(conn, callback.message.chat.id, user['group_id'])
    conn.close()
    do_track_cb(callback, 'language_all')

    await callback.message.edit_text(
        "🔤 Показываю все потоки.\nВернуть выбор — /язык", parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data.startswith('lang:s:'))
async def on_lang_subject(callback: CallbackQuery):
    """
    Второй шаг: конкретные потоки, а не просто преподаватели.

    Один преподаватель нередко ведёт два потока — на сентябрь 2026 таких
    сочетаний 18 из 50. Поэтому в списке сами потоки, а различаются они
    расписанием: «Захарова Д.С. · пн 15:40, ср 12:20».
    """
    user = await check_group(callback)
    if not user:
        return
    wanted = callback.data.split(':', 2)[2]

    conn = get_connection()
    subject = next((s for s, _ in get_stream_subjects(conn, user['group_id'])
                    if subject_hash(s) == wanted), None)
    if not subject:
        conn.close()
        await callback.answer("Язык не найден, попробуй /язык заново")
        return

    variants = get_stream_variants(conn, user['group_id'], subject)

    # Один поток — выбирать нечего
    if len(variants) <= 1:
        v = variants[0] if variants else {'teacher': '', 'subgroup': ''}
        set_user_stream(conn, callback.message.chat.id, user['group_id'],
                        subject, v['teacher'], v['subgroup'])
        conn.close()
        do_track_cb(callback, 'language_set', subject)
        await callback.message.edit_text(
            f"✅ Твой язык: <b>{subject}</b>\n\n"
            f"В расписании останется только он. Изменить — /язык",
            parse_mode=ParseMode.HTML)
        await callback.answer()
        return

    conn.close()

    # Уточняем расписанием только тех преподавателей, у кого больше
    # одного потока: остальным лишний хвост в кнопке ни к чему
    counts = {}
    for v in variants:
        counts[v['teacher']] = counts.get(v['teacher'], 0) + 1

    buttons = []
    for v in variants:
        label = v['teacher'] or 'без преподавателя'
        if counts.get(v['teacher'], 0) > 1 and v['slots']:
            label += f" · {format_slots(v['slots'])}"
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"lang:v:{wanted}:{subject_hash(v['subgroup'])}")])
    buttons.append([InlineKeyboardButton(
        text="Любой преподаватель", callback_data=f"lang:any:{wanted}")])

    await callback.message.edit_text(
        f"🔤 <b>{subject}</b>\n\nВыбери свой поток. Где преподаватель ведёт "
        f"несколько групп, рядом показано время занятий — по нему и узнаешь своё.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith('lang:v:'))
async def on_lang_variant(callback: CallbackQuery):
    user = await check_group(callback)
    if not user:
        return
    _, _, shash, vhash = callback.data.split(':', 3)

    conn = get_connection()
    subject = next((s for s, _ in get_stream_subjects(conn, user['group_id'])
                    if subject_hash(s) == shash), None)
    variant = None
    if subject:
        variant = next((v for v in get_stream_variants(conn, user['group_id'], subject)
                        if subject_hash(v['subgroup']) == vhash), None)
    if subject and variant:
        set_user_stream(conn, callback.message.chat.id, user['group_id'],
                        subject, variant['teacher'], variant['subgroup'])
    conn.close()

    if not (subject and variant):
        await callback.answer("Поток не найден, попробуй /язык заново")
        return

    do_track_cb(callback, 'language_set', f"{subject} / {variant['subgroup']}")
    when = format_slots(variant['slots'], limit=3)
    await callback.message.edit_text(
        f"✅ Твой поток: <b>{subject}</b> — {variant['teacher'] or 'без преподавателя'}\n"
        f"   {when}\n\n"
        f"В расписании останется только он. Изменить — /язык",
        parse_mode=ParseMode.HTML)
    await callback.answer()


@router.callback_query(F.data.startswith('lang:any:'))
async def on_lang_any_teacher(callback: CallbackQuery):
    user = await check_group(callback)
    if not user:
        return
    wanted = callback.data.split(':', 2)[2]

    conn = get_connection()
    subject = next((s for s, _ in get_stream_subjects(conn, user['group_id'])
                    if subject_hash(s) == wanted), None)
    if subject:
        set_user_stream(conn, callback.message.chat.id, user['group_id'], subject, '')
    conn.close()

    if not subject:
        await callback.answer("Язык не найден, попробуй /язык заново")
        return

    do_track_cb(callback, 'language_set', subject)
    await callback.message.edit_text(
        f"✅ Твой язык: <b>{subject}</b>, любой преподаватель.\n\nИзменить — /язык",
        parse_mode=ParseMode.HTML)
    await callback.answer()


# === Реклама / Полезное ===

@router.message(F.text == AD_BUTTON_LABEL)
async def cmd_ad(message: Message, state: FSMContext):
    await state.clear()
    if not AD_FULL_TEXT:
        return
    do_track(message, 'ad_click')
    await message.answer(AD_FULL_TEXT, parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))


# === Статистика (только для админа) ===

@router.message(Command('stats'))
async def cmd_stats(message: Message, state: FSMContext):
    await state.clear()
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
        parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))

# === Расписание преподавателя ===

@router.message(F.text == "👨‍🏫 Преподаватель")
async def cmd_teacher_start(message: Message, state: FSMContext):
    await state.set_state(Search.teacher)
    do_track(message, 'teacher_search')
    await message.answer(
        "👨‍🏫 Напиши фамилию преподавателя (или первые буквы):",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard_for(message.chat.id),
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
async def cmd_change_group(message: Message, state: FSMContext):
    await state.clear()
    do_track(message, 'change_group')
    await show_department_selection(message)


HELP_SECTIONS = {
    'subjects': (
        "📋 <b>Предметы по выбору</b>\n\n"
        "Бывает, на одну пару у группы стоят сразу несколько предметов: "
        "кто-то ходит на один, кто-то на другой. Бот находит такие пары сам.\n\n"
        "Нажми <b>📋 Предметы</b> и отметь свои — в расписании останутся "
        "только они. Ничего не отметил — показывается всё, так что "
        "не потеряешь.\n\n"
        "Сбросить: 📋 Предметы → 🔄 Сбросить всё."
    ),
    'lang': (
        "🔤 <b>Языки</b>\n\n"
        "Языки идут не всей группой: часть учит английский, часть немецкий, "
        "часть французский. У каждого потока свои преподаватель, время "
        "и аудитория, поэтому в обычном расписании их нет вовсе — "
        "бот показывает их блоком «не у всех» под днём.\n\n"
        "Нажми <b>🔤 Мой язык</b> и выбери свой поток — останется только он. "
        "Если преподаватель ведёт несколько групп, рядом показано время: "
        "по нему и узнаешь своё занятие.\n\n"
        "Языки есть только на первом курсе."
    ),
    'empty': (
        "❓ <b>Почему пусто</b>\n\n"
        "🎉 <b>Нет занятий</b> — выходной или в этот день правда ничего нет.\n\n"
        "📭 <b>Расписание ещё не выложено</b> — дальше этой даты факультет "
        "ничего не публиковал. Бот показывает ровно то, что есть на сайте.\n\n"
        "📭 <b>Данных за этот день нет</b> — это уже прошлый семестр, "
        "он не хранится."
    ),
    'group': (
        "👥 <b>Группа и поиск</b>\n\n"
        "Сменить: <b>👥 Сменить группу</b> → отделение → курс → группа.\n"
        "Или просто напиши номер: <b>403</b>, <b>с403</b>, <b>пп201</b>, "
        "<b>мг52МКПП</b>.\n\n"
        "<b>🔍 Найти себя по фамилии</b> — бот сам поставит группу "
        "и отметит твои предметы по выбору.\n\n"
        "<b>👨‍🏫 Преподаватель</b> — покажу, где и когда он ведёт "
        "в ближайшие две недели. Регистр не важен."
    ),
}


def help_menu_keyboard(has_streams: bool) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="📋 Предметы по выбору", callback_data="help:subjects")]
    rows = [row]
    if has_streams:
        rows.append([InlineKeyboardButton(text="🔤 Языки", callback_data="help:lang")])
    rows.append([
        InlineKeyboardButton(text="👥 Группа и поиск", callback_data="help:group"),
        InlineKeyboardButton(text="❓ Почему пусто", callback_data="help:empty"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


HELP_INTRO = (
    "📖 <b>Как пользоваться</b>\n\n"
    "📅 Сегодня, 📆 Завтра, 🗓 Неделя — расписание. Под ним кнопки, "
    "чтобы листать дни и недели.\n"
    "Номер группы можно просто написать: <b>403</b>, <b>пп201</b>.\n\n"
    "О чём подробнее?"
)


def user_has_streams(conn, chat_id: int) -> bool:
    return bool(conn.execute(
        """SELECT 1 FROM subscriptions s JOIN lessons l ON l.group_id = s.group_id
            WHERE s.chat_id = ? AND l.subgroup != '' AND l.date >= date('now') LIMIT 1""",
        (chat_id,)).fetchone())


@router.message(F.text == HELP_BUTTON)
@router.message(Command('помощь', 'help', 'гайд', 'guide'))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    do_track(message, 'help')

    conn = get_connection()
    has_streams = user_has_streams(conn, message.chat.id)
    conn.close()

    await message.answer(HELP_INTRO, parse_mode=ParseMode.HTML,
                         reply_markup=help_menu_keyboard(has_streams))


@router.callback_query(F.data.startswith('help:'))
async def on_help_section(callback: CallbackQuery):
    key = callback.data.split(':', 1)[1]

    if key == 'back':
        conn = get_connection()
        has_streams = user_has_streams(conn, callback.message.chat.id)
        conn.close()
        await callback.message.edit_text(
            HELP_INTRO, parse_mode=ParseMode.HTML,
            reply_markup=help_menu_keyboard(has_streams))
        await callback.answer()
        return

    text = HELP_SECTIONS.get(key)
    if not text:
        await callback.answer("Раздел не найден")
        return

    do_track_cb(callback, 'help_section', key)
    await callback.message.edit_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="← Назад", callback_data="help:back")]]))
    await callback.answer()


# === Поиск: студент, преподаватель, группа ===
#
# Три поиска разведены состояниями (Search). Пока состояния не было, все они
# жили в одном обработчике и различались угадыванием по заглавной букве:
# сначала искался студент и при находке делался return, поэтому поиск
# преподавателя перехватывался однофамильцами-студентами.


async def reply_students(message: Message, query: str) -> bool:
    """Показать найденных студентов. True, если кто-то нашёлся."""
    conn = get_connection()
    found = get_students_by_name(conn, query)
    conn.close()

    if not found:
        return False

    buttons = [[InlineKeyboardButton(
        text=f"{s['full_name']} ({s['group_code']})",
        callback_data=f"bind:{s['id']}",
    )] for s in found]

    await message.answer(
        f"👤 Найдено студентов: {len(found)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    return True


async def reply_teachers(message: Message, query: str) -> bool:
    """Показать найденных преподавателей. True, если кто-то нашёлся."""
    conn = get_connection()
    names = find_teachers_by_name(conn, query)
    conn.close()

    if not names:
        return False

    buttons = [[InlineKeyboardButton(text=name, callback_data=f"tch:{name}")]
               for name in names]
    await message.answer(
        f"👨‍🏫 Найдено преподавателей: {len(names)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    return True


async def reply_groups(message: Message, query: str) -> bool:
    """
    Показать найденные группы. Единственное совпадение выбирается сразу.
    True, если что-то нашлось.
    """
    conn = get_connection()
    rows = find_groups_by_code(conn, normalize_group_query(query))
    conn.close()

    if not rows:
        return False

    if len(rows) == 1:
        conn = get_connection()
        set_user_group(conn, message.chat.id, rows[0]['id'])
        user = get_user_group(conn, message.chat.id)
        conn.close()
        do_track(message, 'set_group', user['group_code'])
        await message.answer(
            f"✅ Группа: <b>{user['group_code']}</b>\n"
            f"   {user['faculty_name']}, {user['department']}",
            parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))
        return True

    buttons = [[InlineKeyboardButton(
        text=f"{g['code']} ({g['department']}, {g['program']})",
        callback_data=f"grp:{g['id']}")] for g in rows]
    await message.answer(f"🔍 Найдено групп: {len(rows)}",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    return True


async def handled_as_group_code(message: Message, state: FSMContext, text: str) -> bool:
    """
    Цифра в запросе однозначно означает код группы: фамилий с цифрами не бывает,
    а код группы без цифр — не встречается.

    Нужно потому, что режим поиска студента снимается только при выборе
    человека из списка. Написал «Найти себя по фамилии», никого не нашёл,
    потом набрал «403» — и без этой ветки номер группы ушёл бы в поиск
    студента и вернул «не нашлась».
    """
    if not any(c.isdigit() for c in text):
        return False

    do_track(message, 'group_query', text)
    if await reply_groups(message, text):
        await state.clear()
    else:
        await message.answer(
            f"🔍 Группа «{text}» не найдена.\nПопробуй <b>👥 Сменить группу</b>.",
            parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))
    return True


@router.message(Search.teacher, F.text & ~F.text.startswith('/'))
async def on_teacher_query(message: Message, state: FSMContext):
    """Ждём фамилию преподавателя — и ищем только преподавателя."""
    text = message.text.strip()
    if text in MAIN_BUTTON_TEXTS:
        return
    if await handled_as_group_code(message, state, text):
        return
    if len(text) < 2:
        await message.answer("Нужно хотя бы две буквы фамилии.")
        return

    do_track(message, 'teacher_query', text)
    if not await reply_teachers(message, text):
        await message.answer(
            f"👨‍🏫 Преподаватель «{text}» не найден.\n"
            f"Проверь написание или напиши только фамилию.",
            parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))


@router.message(Search.student, F.text & ~F.text.startswith('/'))
async def on_student_query(message: Message, state: FSMContext):
    """Ждём фамилию студента — и ищем только студента."""
    text = message.text.strip()
    if text in MAIN_BUTTON_TEXTS:
        return
    if await handled_as_group_code(message, state, text):
        return
    if len(text) < 2:
        await message.answer("Нужно хотя бы две буквы фамилии.")
        return

    do_track(message, 'student_query', text)
    if not await reply_students(message, text):
        await message.answer(
            f"👤 «{text}» в списках не нашлась.\n"
            f"Списки студентов собираются отдельно от расписания и бывают "
            f"неполными — выбери группу через <b>👥 Сменить группу</b>.",
            parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))


@router.message(Search.group, F.text & ~F.text.startswith('/'))
async def on_group_query(message: Message, state: FSMContext):
    """Ждём номер группы — и ищем только группу."""
    text = message.text.strip()
    if text in MAIN_BUTTON_TEXTS:
        return

    do_track(message, 'group_query', text)
    if await reply_groups(message, text):
        await state.clear()
    else:
        await message.answer(
            f"🔍 Группа «{text}» не найдена. Попробуй ещё раз или нажми "
            f"<b>👥 Сменить группу</b>.",
            parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))


@router.message(F.text & ~F.text.startswith('/'))
async def on_text_message(message: Message, state: FSMContext):
    """
    Текст без состояния: человек пишет боту сам, не после нажатия кнопки.

    Различаем не по регистру, а по содержимому: код группы всегда содержит
    цифру, фамилия — никогда. Если цифр нет, ищем и студентов, и
    преподавателей, и показываем обоих — вместо того чтобы угадывать,
    кого человек имел в виду, и молча съедать вторую половину ответа.
    """
    text = message.text.strip()
    if text in MAIN_BUTTON_TEXTS or len(text) < 2:
        return

    if any(c.isdigit() for c in text):
        do_track(message, 'group_query', text)
        if not await reply_groups(message, text):
            await message.answer(
                f"🔍 Группа «{text}» не найдена.\n"
                f"Попробуй <b>👥 Сменить группу</b>.",
                parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))
        return

    do_track(message, 'name_query', text)
    found_students = await reply_students(message, text)
    found_teachers = await reply_teachers(message, text)

    if not found_students and not found_teachers:
        await message.answer(
            f"🔍 По запросу «{text}» ничего не нашлось.\n\n"
            f"Номер группы можно написать прямо так: <b>403</b>, <b>с403</b>, <b>пп201</b>.\n"
            f"Преподавателя — через <b>👨‍🏫 Преподаватель</b>.\n"
            f"Себя — через <b>👥 Сменить группу</b> → «Найти себя по фамилии».",
            parse_mode=ParseMode.HTML, reply_markup=keyboard_for(message.chat.id))


# === Ошибки ===

# Когда последний раз жаловались админу на исключение этого типа.
# Без этого зациклившаяся ошибка превращается в поток одинаковых сообщений.
_last_alert = {}
ALERT_COOLDOWN = timedelta(minutes=10)


@router.error()
async def on_error(event: ErrorEvent):
    """
    Последний рубеж.

    До этого в боте не было ни одного try/except и ни одного обработчика
    ошибок: любое исключение означало, что человек молча не получает ответа.
    Ни он, ни владелец не узнавали, что сломалось (docs/TODO.md §10).
    """
    exc = event.exception
    logging.exception("Ошибка при обработке апдейта", exc_info=exc)

    upd = event.update
    try:
        if getattr(upd, 'message', None):
            await upd.message.answer(
                "😵 Что-то сломалось на моей стороне. Ошибка записана — "
                "попробуйте ещё раз."
            )
        elif getattr(upd, 'callback_query', None):
            await upd.callback_query.answer(
                "Что-то сломалось. Попробуйте ещё раз.", show_alert=True)
    except Exception:
        # Ответить не смогли; трассировка в журнале уже есть, и это главное
        logging.exception("Не удалось сообщить пользователю об ошибке")

    key = type(exc).__name__
    now = datetime.now()
    if ADMIN_CHAT_ID and now - _last_alert.get(key, datetime.min) > ALERT_COOLDOWN:
        _last_alert[key] = now
        # send_admin_alert синхронный (requests). Через to_thread, иначе
        # на время запроса встаёт весь event loop — он в боте один на всех
        await asyncio.to_thread(
            send_admin_alert, f"🔴 <b>Бот — исключение</b>\n{key}: {exc}")

    return True


# === Запуск ===

async def main():
    if not BOT_TOKEN or BOT_TOKEN == 'your_telegram_bot_token_here':
        print("[bot] BOT_TOKEN not set in .env")
        return

    bot = Bot(token=BOT_TOKEN)
    # Состояния держим в памяти: они живут секунды (ввод фамилии) и
    # переживать перезапуск им незачем
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    print("[bot] Запущен. Ctrl+C — остановить.")
    try:
        await dp.start_polling(bot)
    finally:
        # Иначе на выходе остаётся незакрытая HTTP-сессия
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
