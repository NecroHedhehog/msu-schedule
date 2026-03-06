#!/usr/bin/env python3
"""
Запуск Telegram-бота.

Использование:
    python run_bot.py

Перед запуском:
    1. Получи токен у @BotFather
    2. Впиши его в .env: BOT_TOKEN=...
    3. Запусти парсер: python run_parser.py socio
"""

import asyncio
from bot.main import main

if __name__ == '__main__':
    asyncio.run(main())
