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
                os.environ.setdefault(key.strip(), value.strip())

_load_env()

# --- Основные ---

BOT_TOKEN = os.getenv('BOT_TOKEN', '')
DB_PATH = PROJECT_ROOT / os.getenv('DB_PATH', 'data/schedule.db')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', '')
PARSER_REQUEST_DELAY = float(os.getenv('PARSER_REQUEST_DELAY', '2'))

# --- Реклама ---

AD_TEASER = os.getenv('AD_TEASER', '')          # короткая строка под расписанием
AD_FULL_TEXT = os.getenv('AD_FULL_TEXT', '')      # полный текст по кнопке "Полезное"
AD_BUTTON_LABEL = os.getenv('AD_BUTTON_LABEL', '💡 Полезное')

# --- Расписание пар ---

PAIR_TIMES = {
    1: ('09:00', '10:30'),
    2: ('10:45', '12:15'),
    3: ('12:55', '14:25'),
    4: ('14:40', '16:10'),
    5: ('16:25', '17:55'),
    6: ('18:00', '19:30'),
}
