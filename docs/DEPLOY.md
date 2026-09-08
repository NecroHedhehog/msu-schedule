# Разворачивание на сервере

Целевая конфигурация: Linux VPS, бот работает постоянно, парсер запускается
по таймерам. Ниже — systemd, потому что он есть везде и умеет то, чего не
умеет cron: перезапуск при падении, зависимости между юнитами, журнал.

Ресурсы нужны смешные: база на 5700 занятий весит около 1.3 МБ, бот
в поллинге ест десятки мегабайт. Хватит самой дешёвой VPS.

---

## 1. Установка

```bash
sudo apt install -y python3-venv git sqlite3

sudo adduser --system --group --home /opt/msu-schedule msu
sudo -u msu git clone https://github.com/NecroHedhehog/msu-schedule.git /opt/msu-schedule
cd /opt/msu-schedule

sudo -u msu python3 -m venv .venv
sudo -u msu .venv/bin/pip install -r requirements.txt
```

Нужен Python 3.10 или новее.

## 2. Конфигурация

```bash
sudo -u msu cp .env.example .env
sudo -u msu nano .env          # вписать BOT_TOKEN и ADMIN_CHAT_ID
sudo chmod 600 .env
```

`ADMIN_CHAT_ID` обязателен: без него алерты о сбоях парсера уходят в консоль,
то есть в никуда.

## 3. Первый сбор данных

По порядку, каждая команда отдельно — так видно, где не поехало:

```bash
cd /opt/msu-schedule
sudo -u msu .venv/bin/python run_parser.py socio      # ~2 мин
sudo -u msu .venv/bin/python run_parser.py teachers   # ~10 мин
sudo -u msu .venv/bin/python run_parser.py students   # ~40 мин, можно позже
```

Без `teachers` в расписании не будет ни одного имени: на страницах групп
преподавателя нет вовсе (см. [SITE.md §5](SITE.md)).

Если сайт лежит — не сидеть и не ждать вручную:

```bash
sudo -u msu .venv/bin/python scripts/wait_for_site.py --then socio
```

## 4. Бот как сервис

`/etc/systemd/system/msu-bot.service`:

```ini
[Unit]
Description=msu-schedule telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=msu
Group=msu
WorkingDirectory=/opt/msu-schedule
ExecStart=/opt/msu-schedule/.venv/bin/python run_bot.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Бот пишет только в data/ — остальное можно закрыть
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/msu-schedule/data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now msu-bot
sudo systemctl status msu-bot
```

## 5. Парсер по таймерам

Данные живут с разной скоростью, поэтому проходы разнесены. Расписание правят
в течение семестра — его гоняем часто; списки студентов не меняются вовсе.

`/etc/systemd/system/msu-parser@.service` — один шаблон на все режимы:

```ini
[Unit]
Description=msu-schedule parser (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=msu
Group=msu
WorkingDirectory=/opt/msu-schedule
ExecStart=/opt/msu-schedule/.venv/bin/python run_parser.py %i
TimeoutStartSec=3600
StandardOutput=journal
StandardError=journal

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/msu-schedule/data
```

`/etc/systemd/system/msu-parser-socio.timer`:

```ini
[Unit]
Description=Собирать расписание групп

[Timer]
OnCalendar=*-*-* 07,13,19:10
Persistent=true
RandomizedDelaySec=600
Unit=msu-parser@socio.service

[Install]
WantedBy=timers.target
```

`/etc/systemd/system/msu-parser-teachers.timer`:

```ini
[Unit]
Description=Собирать преподавателей через кафедры

[Timer]
OnCalendar=*-*-* 04:30
Persistent=true
RandomizedDelaySec=900
Unit=msu-parser@teachers.service

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now msu-parser-socio.timer msu-parser-teachers.timer
systemctl list-timers 'msu-*'
```

`RandomizedDelaySec` тут не украшение: сайт древний, и попадать в него строго
по расписанию каждый раз незачем.

Проход по студентам в таймеры не ставится — он на сорок минут и три с половиной
тысячи запросов. Запускать руками раз в семестр, а на середине семестра, если
понадобится, с `--resume`.

## 6. Проверка свежести

`/etc/systemd/system/msu-freshness.service`:

```ini
[Unit]
Description=msu-schedule freshness check

[Service]
Type=oneshot
User=msu
Group=msu
WorkingDirectory=/opt/msu-schedule
ExecStart=/opt/msu-schedule/.venv/bin/python check_freshness.py
```

`/etc/systemd/system/msu-freshness.timer`:

```ini
[Unit]
Description=Проверять, не протухли ли данные

[Timer]
OnCalendar=*-*-* 09,21:00
Persistent=true
Unit=msu-freshness.service

[Install]
WantedBy=timers.target
```

Скрипт смотрит в `parse_log` и шлёт алерт, если последний зачтённый прогон
был давно. Порог берётся из `PARSER_INTERVAL_HOURS` с двукратным запасом.
Оборванный прогон (статус `partial`) свежим не считается — это важно, иначе
сломанный сбор выглядел бы как рабочий.

## 7. Бэкапы

База маленькая, копировать её можно хоть ежедневно. Правильно — через
`.backup`, а не `cp`: при включённом WAL файловая копия может не содержать
последних транзакций.

`/etc/systemd/system/msu-backup.service`:

```ini
[Unit]
Description=msu-schedule database backup

[Service]
Type=oneshot
User=msu
Group=msu
WorkingDirectory=/opt/msu-schedule
ExecStart=/bin/sh -c 'sqlite3 data/schedule.db ".backup data/backups/schedule-$(date +%%F).db" && find data/backups -name "schedule-*.db" -mtime +14 -delete'
```

С таймером на `OnCalendar=*-*-* 03:00`. Каталог создать заранее:

```bash
sudo -u msu mkdir -p /opt/msu-schedule/data/backups
```

## 8. Логи

```bash
journalctl -u msu-bot -f                    # бот вживую
journalctl -u 'msu-parser@*' --since today  # прогоны за сегодня
```

Парсер печатает по строке на запрос — при полном проходе это тысячи строк.
Если журнал распухает, ограничьте:

```ini
# в msu-parser@.service
LogRateLimitIntervalSec=0
```

или заведите `SystemMaxUse` в `/etc/systemd/journald.conf`.

## 9. Что мониторить

Главный индикатор — таблица `parse_log`. Смотреть так:

```bash
sqlite3 -header -column data/schedule.db \
  "SELECT created_at, faculty_code, status, lessons_count, groups_count, message
     FROM parse_log ORDER BY id DESC LIMIT 10"
```

| Статус | Что значит | Реакция |
|---|---|---|
| `ok` | прогон прошёл чисто | ничего |
| `warning` | данные обновились, но есть оговорки | прочитать `message` |
| `partial` | прогон оборвался, сохранено частично | повторить, для студентов — `--resume` |
| `error` | не собрано ничего | смотреть журнал |

В `message` лежат счётчики: сколько запросов провалилось, сколько блоков
не распознано, сколько заголовков починено, какие группы подозрительно пусты.
**Растущее число нераспознанных блоков означает, что сайт изменили** — надо
идти смотреть разметку и обновлять [SITE.md](SITE.md).

Алерты в Telegram приходят сами: на `error`, на `partial` и на устаревание.

## 10. Обновление

```bash
cd /opt/msu-schedule
sudo -u msu git pull
sudo -u msu .venv/bin/pip install -r requirements.txt
sudo -u msu .venv/bin/python -m unittest discover -s tests -t .
sudo systemctl restart msu-bot
```

Тесты сеть не трогают и проходят за несколько секунд — гонять перед каждым
перезапуском не лишнее.

## 11. Смена семестра

Коды групп на сайте переиспользуются между наборами: весенний `с101` — это
осенний `с201`. Если не почистить, под одним кодом окажутся занятия двух
разных наборов (см. [SITE.md §9.3](SITE.md)).

```bash
sudo -u msu .venv/bin/python scripts/purge_old.py                        # посмотреть
sudo -u msu .venv/bin/python scripts/purge_old.py --before ГГГГ-ММ-ДД --students --yes
sudo -u msu .venv/bin/python run_parser.py socio
sudo -u msu .venv/bin/python run_parser.py teachers
sudo -u msu .venv/bin/python run_parser.py students
```

Скрипт кладёт резервную копию базы рядом и по умолчанию ничего не удаляет.
Привязки пользователей к студентам при `--students` сбрасываются — людям
придётся заново найти себя по фамилии.

## 12. Если что-то не так

**Бот молчит.** `systemctl status msu-bot`. Частое: неверный `BOT_TOKEN`
или второй экземпляр бота на том же токене — Telegram отдаёт поллинг только
одному.

**Парсер пишет `FetchError` на главной.** Сайт лежит. Проверить:
`python scripts/wait_for_site.py --check-only`. Само пройдёт.

**`parse_log` показывает `partial` подряд несколько раз.** Сайт отвечает
через раз. Увеличить `PARSER_MAX_ATTEMPTS` и `PARSER_RETRY_BACKOFF`, либо
запускать через `wait_for_site.py --then`.

**Резко выросло число нераспознанных блоков.** Сайт изменили. Открыть
страницу группы в браузере, сравнить с [SITE.md §3](SITE.md), поправить
разбор и добавить тест в `tests/`.

**Расписание пустое, хотя прогон прошёл.** Проверить даты:
`SELECT MIN(date), MAX(date) FROM lessons`. Парсер берёт текущий месяц
и следующий — в конце семестра там может быть пусто по-честному.

**База заблокирована.** Долгий прогон и бот пишут одновременно.
WAL включён, обычно проходит само; если повторяется — разнести таймеры
или перевести бота на одно долгоживущее соединение (см. [TODO.md](TODO.md)).
