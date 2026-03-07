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