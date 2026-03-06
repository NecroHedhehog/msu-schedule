# MSU Schedule — Расписание МГУ

Сервис расписания для студентов МГУ: парсер + Telegram-бот + веб-сайт.

## Быстрый старт

### 1. Установка

```bash
# Клонируй проект (или распакуй архив)
cd msu-schedule

# Установи зависимости
pip install -r requirements.txt

# Создай конфиг
cp .env.example .env
```

### 2. Тест на локальном файле (без обращения к сайту)

Открой расписание своей группы в браузере, нажми **Ctrl+U**, сохрани как `socio.html` в папку проекта. Затем:

```bash
python run_parser.py --test socio.html
```

Должен вывести список занятий с датами, парами, аудиториями.

### 3. Полный запуск парсера (ходит на сайт)

```bash
python run_parser.py socio
```

Парсер сам:
- зайдёт на cacs.socio.msu.ru
- найдёт все группы
- скачает расписание каждой за текущий и следующий месяц
- сохранит в базу `data/schedule.db`

### 4. Проверка базы

```bash
# Посмотреть что в базе:
python -c "
import sqlite3
conn = sqlite3.connect('data/schedule.db')
print('Группы:', conn.execute('SELECT code FROM groups_').fetchall())
print('Занятий:', conn.execute('SELECT COUNT(*) FROM lessons').fetchone()[0])
conn.close()
"
```

## Структура проекта

```
msu-schedule/
├── core/
│   ├── config.py        # Загрузка .env, константы
│   └── database.py      # SQLite: таблицы, запросы
├── parsers/
│   ├── base.py          # Базовый класс парсера
│   └── socio.py         # Парсер соцфака
├── bot/                 # Telegram-бот (TODO)
├── web/                 # Веб-сайт (TODO)
├── deploy/              # systemd-сервисы (TODO)
├── run_parser.py        # Точка запуска парсера
├── requirements.txt
├── .env.example         # Шаблон конфига
└── .gitignore
```

## Статус

- [x] Парсер соцфака
- [ ] Парсер ФГУ
- [ ] Telegram-бот
- [ ] Веб-сайт
- [ ] Деплой на VPS
- [ ] Уведомления об изменениях
