"""
Алерты в Telegram для администратора.
Используется парсером (requests) и ботом (проверка актуальности данных).
"""

import requests
from core.config import (BOT_TOKEN, ADMIN_CHAT_ID, ALERT_ON_SUCCESS,
                         ALERT_REPEAT_HOURS)


def _first_time(kind: str, subject_key: str, text: str) -> bool:
    """
    Не уходил ли ровно такой же алерт совсем недавно.

    Своё соединение, а не переданное: алерты шлёт и парсер, и проверка
    свежести, и вызываются они из разных мест, где conn под рукой не всегда.

    Если база недоступна, отвечаем «шли». Лишнее сообщение — неприятность,
    потерянная тревога — отказ.
    """
    try:
        from core.database import get_connection, claim_alert
        conn = get_connection()
        try:
            return claim_alert(conn, kind, subject_key, text, ALERT_REPEAT_HOURS)
        finally:
            conn.close()
    except Exception as e:
        print(f"[alert] Не удалось проверить повтор ({e}), шлю на всякий случай")
        return True


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
    """
    Парсинг прошёл успешно.

    По умолчанию молчит: успешных прогонов несколько в день, и если слать
    каждый, то в потоке «всё хорошо» теряется то единственное сообщение,
    ради которого алерты и заводились. Включается ALERT_ON_SUCCESS=1.
    Что прогон был и чем кончился, всегда видно в parse_log.
    """
    if not ALERT_ON_SUCCESS:
        return
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
    """
    Парсинг отработал, но что-то подозрительно (мало данных и т.д.).

    Одно и то же предупреждение повторяется не чаще ALERT_REPEAT_HOURS:
    пустые группы держатся неделями, и напоминание о них трижды в день —
    шум. Изменился список — текст другой, и сообщение придёт сразу.
    """
    if not _first_time('parse_warning', faculty_code, message):
        print(f"[alert] Такое же предупреждение по {faculty_code} уже уходило, молчу")
        return
    send_admin_alert(
        f"⚠️ <b>Парсер [{faculty_code}] — предупреждение</b>\n"
        f"{message}"
    )


def alert_stale_data(faculty_code: str, hours_since: float):
    """
    Данные устарели — парсер давно не запускался.

    Часы в ключ повтора не входят: они растут с каждой проверкой, и текст
    каждый раз получался бы новым, то есть повтор не ловился бы вовсе.
    """
    if not _first_time('stale', faculty_code, 'устарели'):
        print(f"[alert] Про устаревание {faculty_code} уже сообщал, молчу")
        return
    send_admin_alert(
        f"⏰ <b>Данные [{faculty_code}] устарели!</b>\n"
        f"Последний успешный парсинг: {hours_since:.0f} ч. назад"
    )
