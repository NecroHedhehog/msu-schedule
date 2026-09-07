"""Загрузка конфигурации из .env файла."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def _load_env():
    env_path = PROJECT_ROOT / '.env'
    if not env_path.exists():
        print(f"[config] .env not found ({env_path})")
        return
    with open(env_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, value = line.split('=', 1)
                value = value.strip().replace('\\n', '\n')
                os.environ.setdefault(key.strip(), value)

_load_env()

# --- Основные ---

BOT_TOKEN = os.getenv('BOT_TOKEN', '')
DB_PATH = PROJECT_ROOT / os.getenv('DB_PATH', 'data/schedule.db')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', '')
PARSER_REQUEST_DELAY = float(os.getenv('PARSER_REQUEST_DELAY', '2'))
PARSER_REQUEST_TIMEOUT = float(os.getenv('PARSER_REQUEST_TIMEOUT', '30'))
PARSER_INTERVAL_HOURS = float(os.getenv('PARSER_INTERVAL_HOURS', '6'))

# --- Устойчивость парсера ---
# Сайт факультета регулярно недоступен, поэтому один таймаут не должен
# означать потерю данных за группу.

# Сколько всего попыток на один запрос (1 = без повторов)
PARSER_MAX_ATTEMPTS = int(os.getenv('PARSER_MAX_ATTEMPTS', '4'))
# База экспоненциального бэкоффа в секундах: 3, 6, 12, ...
PARSER_RETRY_BACKOFF = float(os.getenv('PARSER_RETRY_BACKOFF', '3'))
# Порог "подозрительно мало занятий" — на группу, а не на факультет
MIN_LESSONS_PER_GROUP = int(os.getenv('MIN_LESSONS_PER_GROUP', '10'))
# Не затирать расписание группы, если пришло меньше этой доли от того,
# что уже лежит в базе за те же даты (сайт икнул и отдал огрызок)
SHRINK_GUARD_RATIO = float(os.getenv('SHRINK_GUARD_RATIO', '0.5'))

# --- Реклама ---

AD_TEASER = os.getenv('AD_TEASER', '')
AD_FULL_TEXT = os.getenv('AD_FULL_TEXT', '')
AD_BUTTON_LABEL = os.getenv('AD_BUTTON_LABEL', '💡 Полезное')

# --- Расписание пар ---

PAIR_TIMES = {
    1: ('09:00', '10:30'),
    2: ('10:40', '12:10'),
    3: ('12:20', '13:50'),
    4: ('14:00', '15:30'),
    5: ('15:40', '17:10'),
    6: ('17:20', '18:50'),
}

# По средам МФК (пары 4-5) в другое время
PAIR_TIMES_WED_MFK = {
    4: ('15:10', '16:40'),
    5: ('17:00', '18:30'),
}