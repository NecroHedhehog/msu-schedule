"""
Загрузка конфигурации из .env файла.
"""

import os
from pathlib import Path

# Корень проекта — папка, где лежит этот файл (core/), на уровень выше
PROJECT_ROOT = Path(__file__).parent.parent

# Загружаем .env вручную (без лишних зависимостей)
def _load_env():
    env_path = PROJECT_ROOT / '.env'
    if not env_path.exists():
        print(f"⚠️  Файл .env не найден ({env_path})")
        print(f"   Скопируй .env.example → .env и заполни значения")
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

# --- Настройки ---

BOT_TOKEN = os.getenv('BOT_TOKEN', '')
DB_PATH = PROJECT_ROOT / os.getenv('DB_PATH', 'data/schedule.db')
ADMIN_CHAT_ID = os.getenv('ADMIN_CHAT_ID', '')
PARSER_REQUEST_DELAY = float(os.getenv('PARSER_REQUEST_DELAY', '2'))

# Время пар на соцфаке (и филфаке — одинаковое)
PAIR_TIMES = {
    1: ('09:00', '10:30'),
    2: ('10:45', '12:15'),
    3: ('12:55', '14:25'),
    4: ('14:40', '16:10'),
    5: ('16:25', '17:55'),
    6: ('18:00', '19:30'),
}
