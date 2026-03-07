"""
Алерты в Telegram для администратора.
Используется парсером (requests) и ботом (проверка актуальности данных).
"""

import requests
from core.config import BOT_TOKEN, ADMIN_CHAT_ID


def send_admin_alert(text: str) -> bool:
    """
    Отправить сообщение админу через Telegram Bot API.
    Работает без aiogram — просто HTTP-запрос.
    Возвращает True если отправлено.
    """
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        print(f"[alert] Не настроен BOT_TOKEN или ADMIN_CHAT_ID, пишу в консоль:")
        print(f"[alert] {text}")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={
            'chat_id': ADMIN_CHAT_ID,
            'text': text,
            'parse_mode': 'HTML',
        }, timeout=10)
        if resp.status_code == 200:
            return True
        else:
            print(f"[alert] Telegram API error: {resp.status_code} {resp.text[:200]}")
            return False
    except requests.RequestException as e:
        print(f"[alert] Не удалось отправить: {e}")
        return False


def alert_parse_ok(faculty_code: str, groups_count: int, lessons_count: int):
    """Парсинг прошёл успешно."""
    send_admin_alert(
        f"✅ <b>Парсер [{faculty_code}]</b>\n"
        f"Групп: {groups_count}, занятий: {lessons_count}"
    )


def alert_parse_error(faculty_code: str, error: str):
    """Парсинг упал или вернул ноль данных."""
    send_admin_alert(
        f"🔴 <b>Парсер [{faculty_code}] — ошибка!</b>\n"
        f"{error}"
    )


def alert_parse_warning(faculty_code: str, message: str):
    """Парсинг отработал, но что-то подозрительно (мало данных и т.д.)."""
    send_admin_alert(
        f"⚠️ <b>Парсер [{faculty_code}] — предупреждение</b>\n"
        f"{message}"
    )


def alert_stale_data(faculty_code: str, hours_since: float):
    """Данные устарели — парсер давно не запускался."""
    send_admin_alert(
        f"⏰ <b>Данные [{faculty_code}] устарели!</b>\n"
        f"Последний успешный парсинг: {hours_since:.0f} ч. назад"
    )
