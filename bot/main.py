"""
Telegram-бот для расписания МГУ.

Команды:
  /start    — начало, выбор группы
  /сегодня  — расписание на сегодня
  /завтра   — расписание на завтра
  /неделя   — расписание на неделю
  /предметы — выбрать свои предметы (фильтр предметов по выбору)
  /группа   — сменить группу
  /помощь   — список команд
"""

import asyncio
import hashlib
import logging
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.enums import ParseMode

from core.config import BOT_TOKEN
from core.database import (
    get_connection, get_user_group, set_user_group,
    get_filtered_lessons, get_lessons_for_week,
    get_conflicting_subjects, get_user_subjects, toggle_user_subject,
)
from bot.formatting import (
    format_day_schedule, format_week_schedule, format_subject_list,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = Router()


# === Утилиты ===

def subject_hash(subject: str) -> str:
    """Короткий хеш названия предмета для callback_data (макс 64 байта)."""
    return hashlib.md5(subject.encode()).hexdigest()[:10]


def search_groups(query: str) -> list:
    """Поиск групп по введённому тексту."""
    conn = get_connection()
    query_lower = query.lower().strip()
    rows = conn.execute(
        """SELECT g.id, g.code, g.department, g.program,
                  f.name as faculty_name
           FROM groups_ g
           JOIN faculties f ON g.faculty_id = f.id
           WHERE LOWER(g.code) LIKE ?
           ORDER BY g.code
           LIMIT 20""",
        (f'%{query_lower}%',)
    ).fetchall()
    conn.close()
    return rows


def require_group(func):
    """Декоратор: проверить что пользователь выбрал группу."""
    async def wrapper(message: Message):
        conn = get_connection()
        user = get_user_group(conn, message.chat.id)
        conn.close()
        if not user:
            await message.answer(
                "⚠️ Сначала выбери группу!\n"
                "Напиши номер группы, например: <b>с403</b>",
                parse_mode=ParseMode.HTML,
            )
            return
        await func(message, user)
    return wrapper


# === Команды ===

@router.message(CommandStart())
async def cmd_start(message: Message):
    conn = get_connection()
    user = get_user_group(conn, message.chat.id)
    conn.close()

    if user:
        await message.answer(
            f"👋 С возвращением! Твоя группа: <b>{user['group_code']}</b>\n\n"
            f"📌 Команды:\n"
            f"  /сегодня — расписание на сегодня\n"
            f"  /завтра — на завтра\n"
            f"  /неделя — на неделю\n"
            f"  /предметы — выбрать свои предметы\n"
            f"  /группа — сменить группу",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            "👋 Привет! Я бот расписания МГУ.\n\n"
            "Напиши номер своей группы, например:\n"
            "  <b>с403</b>, <b>пп201</b>, <b>мг52МКПП</b>",
            parse_mode=ParseMode.HTML,
        )


@router.message(Command('группа', 'group'))
async def cmd_change_group(message: Message):
    await message.answer(
        "Напиши номер новой группы, например:\n"
        "  <b>с403</b>, <b>пп201</b>, <b>мг52МКПП</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command('помощь', 'help'))
async def cmd_help(message: Message):
    await message.answer(
        "📌 <b>Команды:</b>\n\n"
        "/сегодня — расписание на сегодня\n"
        "/завтра — расписание на завтра\n"
        "/неделя — расписание на неделю\n"
        "/предметы — выбрать свои предметы\n"
        "/группа — сменить группу\n"
        "/помощь — эта справка",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command('сегодня', 'today'))
@require_group
async def cmd_today(message: Message, user: dict):
    today = date.today()
    conn = get_connection()
    lessons = get_filtered_lessons(
        conn, user['group_id'], today.strftime('%Y-%m-%d'), message.chat.id
    )
    conn.close()

    text = f"👥 Группа <b>{user['group_code']}</b>\n\n"
    text += format_day_schedule(lessons, today)

    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command('завтра', 'tomorrow'))
@require_group
async def cmd_tomorrow(message: Message, user: dict):
    tomorrow = date.today() + timedelta(days=1)
    conn = get_connection()
    lessons = get_filtered_lessons(
        conn, user['group_id'], tomorrow.strftime('%Y-%m-%d'), message.chat.id
    )
    conn.close()

    text = f"👥 Группа <b>{user['group_code']}</b>\n\n"
    text += format_day_schedule(lessons, tomorrow)

    await message.answer(text, parse_mode=ParseMode.HTML)


@router.message(Command('неделя', 'week'))
@require_group
async def cmd_week(message: Message, user: dict):
    today = date.today()
    # Начало недели (понедельник)
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    conn = get_connection()
    all_lessons = get_lessons_for_week(
        conn, user['group_id'],
        monday.strftime('%Y-%m-%d'),
        sunday.strftime('%Y-%m-%d'),
    )

    # Фильтруем по выбранным предметам
    user_subj = get_user_subjects(conn, message.chat.id, user['group_id'])
    conn.close()

    # Группируем по дням
    from collections import defaultdict
    from datetime import datetime
    days = defaultdict(list)
    for l in all_lessons:
        d = datetime.strptime(l['date'], '%Y-%m-%d').date()
        # Фильтрация
        if user_subj:
            # Проверяем: если у этой пары несколько предметов, показываем только выбранные
            days[d].append(l)
        else:
            days[d].append(l)

    # Добавляем пустые дни (Пн-Сб)
    for i in range(6):
        d = monday + timedelta(days=i)
        if d not in days:
            days[d] = []

    text = f"👥 Группа <b>{user['group_code']}</b>\n\n"
    text += format_week_schedule(days)

    # Telegram ограничивает сообщения 4096 символами
    if len(text) > 4000:
        # Разбиваем на части по дням
        header = f"👥 Группа <b>{user['group_code']}</b>\n\n"
        await message.answer(header + "📅 Расписание на неделю:", parse_mode=ParseMode.HTML)
        for d in sorted(days.keys()):
            day_text = format_day_schedule(days[d], d)
            await message.answer(day_text, parse_mode=ParseMode.HTML)
    else:
        await message.answer(text, parse_mode=ParseMode.HTML)


# === Выбор предметов ===

@router.message(Command('предметы', 'subjects'))
@require_group
async def cmd_subjects(message: Message, user: dict):
    conn = get_connection()
    conflicts = get_conflicting_subjects(conn, user['group_id'])
    selected = get_user_subjects(conn, message.chat.id, user['group_id'])
    conn.close()

    if not conflicts:
        await message.answer(
            "✅ В расписании нет предметов по выбору — всё однозначно!",
            parse_mode=ParseMode.HTML,
        )
        return

    # Создаём кнопки для каждого предмета
    buttons = []
    for s in conflicts:
        check = '✅' if s['subject'] in selected else '⬜️'
        # Обрезаем название для кнопки
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
        InlineKeyboardButton(text="✅ Готово", callback_data="subj:done")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        f"📋 <b>Предметы по выбору</b> (группа {user['group_code']})\n\n"
        "Нажми на предмет, чтобы добавить или убрать.\n"
        "Выбранные будут показываться в расписании,\n"
        "остальные — скрыты.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith('subj:'))
async def on_subject_toggle(callback: CallbackQuery):
    data = callback.data.split(':', 1)[1]

    if data == 'done':
        await callback.message.edit_text(
            "✅ Настройки предметов сохранены!\n"
            "Используй /сегодня, /завтра, /неделя",
            parse_mode=ParseMode.HTML,
        )
        await callback.answer()
        return

    conn = get_connection()
    user = get_user_group(conn, callback.message.chat.id)
    if not user:
        await callback.answer("Сначала выбери группу!")
        conn.close()
        return

    # Найти предмет по хешу
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

    # Переключить
    is_selected = toggle_user_subject(
        conn, callback.message.chat.id, user['group_id'], target['subject']
    )

    # Обновить кнопки
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
        InlineKeyboardButton(text="✅ Готово", callback_data="subj:done")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    status = "добавлен ✅" if is_selected else "убран ⬜️"
    await callback.answer(f"Предмет {status}")

    await callback.message.edit_reply_markup(reply_markup=keyboard)


# === Выбор группы (текстовый поиск) ===

@router.callback_query(F.data.startswith('grp:'))
async def on_group_select(callback: CallbackQuery):
    group_id = int(callback.data.split(':')[1])

    conn = get_connection()
    set_user_group(conn, callback.message.chat.id, group_id)
    user = get_user_group(conn, callback.message.chat.id)
    conn.close()

    await callback.message.edit_text(
        f"✅ Группа установлена: <b>{user['group_code']}</b>\n"
        f"   {user['faculty_name']}, {user['department']}\n\n"
        f"📌 Команды:\n"
        f"  /сегодня — расписание на сегодня\n"
        f"  /завтра — на завтра\n"
        f"  /неделя — на неделю\n"
        f"  /предметы — выбрать свои предметы",
        parse_mode=ParseMode.HTML,
    )
    await callback.answer()


@router.message(F.text & ~F.text.startswith('/'))
async def on_text_message(message: Message):
    """Обработка текстовых сообщений — поиск группы."""
    text = message.text.strip()

    # Ищем группы
    groups = search_groups(text)

    if not groups:
        await message.answer(
            f"🔍 Группа «{text}» не найдена.\n"
            "Попробуй ещё раз, например: <b>с403</b>, <b>пп201</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if len(groups) == 1:
        # Точное совпадение — сразу устанавливаем
        g = groups[0]
        conn = get_connection()
        set_user_group(conn, message.chat.id, g['id'])
        conn.close()

        await message.answer(
            f"✅ Группа установлена: <b>{g['code']}</b>\n"
            f"   {g['faculty_name']}, {g['department']}\n\n"
            f"📌 Команды:\n"
            f"  /сегодня — расписание на сегодня\n"
            f"  /завтра — на завтра\n"
            f"  /неделя — на неделю\n"
            f"  /предметы — выбрать свои предметы",
            parse_mode=ParseMode.HTML,
        )
        return

    # Несколько совпадений — показываем кнопки
    buttons = []
    for g in groups:
        label = f"{g['code']} ({g['department']})"
        buttons.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"grp:{g['id']}",
            )
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        f"🔍 Найдено {len(groups)} групп. Выбери свою:",
        reply_markup=keyboard,
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
