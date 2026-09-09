# Разворачивание на сервере

Целевая конфигурация: Linux VPS, бот работает постоянно, парсер запускается
по таймерам. Ниже — systemd, потому что он есть везде и умеет то, чего не
умеет cron: перезапуск при падении, зависимости между юнитами, журнал.

Ресурсы нужны смешные: база на 6800 занятий весит 1.8 МБ, бот в поллинге
ест десятки мегабайт. Хватит самой дешёвой VPS.

## Чек-лист

Отметить по ходу — каждый пункт раскрыт ниже:

- [ ] Python 3.10+, пользователь `msu`, клон в `/opt/msu-schedule` — §1
- [ ] `.env` скопирован руками, `chmod 600`, в нём `BOT_TOKEN` и `ADMIN_CHAT_ID` — §2
- [ ] решено: базу переносим или собираем заново — §2.1
- [ ] прогоны `socio` → `subgroups` → `teachers` → `students`, в этом порядке — §3
- [ ] `msu-bot.service` запущен и переживает `reboot` — §4
- [ ] таймеры `socio`, `subgroups`, `teachers` включены — §5
- [ ] `msu-freshness.timer` включён, алерт в Telegram проверен — §6
- [ ] бэкапы в `data/backups/` идут и чистятся — §7
- [ ] в `parse_log` последний прогон `ok`, а не `warning`/`partial` — §9

Отдельно: **дата смены семестра**. Коды групп на сайте переиспользуются,
и без чистки под одним кодом смешаются два набора — §11.

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

## 2.1. Что не приезжает вместе с кодом

`git clone` привозит только код. Намеренно не в репозитории:

| Что | Где лежит | Как получить на сервере |
|---|---|---|
| `.env` | корень проекта | скопировать руками или собрать из `.env.example` |
| `data/schedule.db` | `data/` | либо собрать парсером заново, либо перенести |
| `__pycache__/` | везде | не нужно |

В `.env` боевой токен бота — он не должен попасть в git ни при каких
обстоятельствах. Переносить только по scp, правами `600`.

**Базу переносить или собирать заново?** Расписание собирается за четверть
часа (`socio` + `subgroups` + `teachers`), и свежее всегда лучше. Но в базе, кроме
расписания, лежит то, что парсером не восстанавливается: подписки
пользователей на группы, их выбранные предметы, привязки к студентам и лог
действий. Если бот уже кем-то используется, база переносится:

```bash
# на рабочей машине: снять честную копию (не cp — при WAL потеряете хвост)
sqlite3 data/schedule.db ".backup /tmp/schedule.db"
scp /tmp/schedule.db сервер:/opt/msu-schedule/data/schedule.db

# на сервере
sudo chown msu:msu /opt/msu-schedule/data/schedule.db
```

На Windows `sqlite3.exe` обычно не установлен — там то же самое питоном:

```powershell
python -c "import sqlite3; s=sqlite3.connect('data/schedule.db'); d=sqlite3.connect('C:/Temp/schedule.db'); s.backup(d); d.close(); s.close()"
```

Бота на время переноса остановить (`systemctl stop msu-bot`, на рабочей
машине — Ctrl+C в окне `run_bot.py`), иначе он допишет в старый файл после
снятия копии.

Если бот ещё никем не используется — проще собрать с нуля, шаг 3.

## 3. Первый сбор данных

По порядку, каждая команда отдельно — так видно, где не поехало:

```bash
cd /opt/msu-schedule
sudo -u msu .venv/bin/python run_parser.py socio      # ~2 мин
sudo -u msu .venv/bin/python run_parser.py subgroups  # ~4 мин, строго после socio
sudo -u msu .venv/bin/python run_parser.py teachers   # ~10 мин
sudo -u msu .venv/bin/python run_parser.py students   # ~40 мин, можно позже
```

Порядок не произвольный, и пропуск любого прохода виден пользователю:

| Пропустили | Что сломается |
|---|---|
| `socio` | нет расписания вообще |
| `subgroups` | у первого курса нет языков, кнопка 🔤 Мой язык не появится |
| `teachers` | в расписании нет ни одного имени: на страницах групп преподавателя нет вовсе ([SITE.md §5](SITE.md)) |
| `students` | не работает поиск по фамилии и автоотметка предметов по выбору |

`subgroups` идёт **после** `socio`: он сохраняет только те занятия, которых
у группы ещё нет, и для этого расписание группы должно уже лежать в базе.
Обратный порядок молча даст пустой результат.

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

### Часовой пояс

Бот берёт дату наивным `date.today()`, а факультет московский. Если сервер
стоит не в московском поясе, «Сегодня» будет врать в часы расхождения:
на амстердамской VPS (UTC+2) с полуночи до часу ночи по Москве кнопка
показывала вчерашний день.

Менять системный пояс правильно не всегда — на машине могут жить чужие
сервисы. Достаточно задать его нашим юнитам:

```bash
for u in msu-bot.service 'msu-parser@.service'; do
  sudo mkdir -p "/etc/systemd/system/$u.d"
  printf '[Service]\nEnvironment=TZ=Europe/Moscow\n' | \
    sudo tee "/etc/systemd/system/$u.d/timezone.conf"
done
sudo systemctl daemon-reload && sudo systemctl restart msu-bot
```

Проверить: `sudo -u msu .venv/bin/python -c "from datetime import datetime; print(datetime.now())"`
под юнитом должно дать московское время.

**`OnCalendar` в таймерах это не покрывает** — таймеры считают время
в системном поясе. На сервере в UTC+2 расписание из §5 срабатывает
в 08:10/14:10/20:10 по Москве. Если нужны ровно московские часы, либо
сдвиньте `OnCalendar`, либо припишите таймерам `Timezone=Europe/Moscow`
(systemd 252+).

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

`/etc/systemd/system/msu-parser-subgroups.timer`:

```ini
[Unit]
Description=Собирать языковые потоки первого курса

[Timer]
OnCalendar=*-*-* 05:30
Persistent=true
RandomizedDelaySec=600
Unit=msu-parser@subgroups.service

[Install]
WantedBy=timers.target
```

Раз в сутки хватает: потоки заводят в начале семестра и потом почти не
трогают. Частые прогоны `socio` их не затирают — `save_lessons` удаляет
только занятия с пустой колонкой `subgroup`, и это сделано именно ради
этого разделения.

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
sudo systemctl enable --now msu-parser-socio.timer \
                            msu-parser-subgroups.timer \
                            msu-parser-teachers.timer
systemctl list-timers 'msu-*'
```

`RandomizedDelaySec` тут не украшение: сайт древний, и попадать в него строго
по расписанию каждый раз незачем.

Проход по студентам в таймеры не ставится — он на сорок минут и три с половиной
тысячи запросов. Запускать руками раз в семестр, а на середине семестра, если
понадобится, с `--resume`.

**`TimeoutStartSec=3600` этому проходу мал.** Замер 09.09.2026: прогон дошёл
до 639 студентов из 1101 и был убит systemd ровно через час. «Сорок минут»
из этого файла — оценка по хорошему дню, реальный сайт медленнее. Поднимать
только этому экземпляру шаблона, остальным режимам часа хватает:

```bash
sudo mkdir -p '/etc/systemd/system/msu-parser@students.service.d'
printf '[Service]\nTimeoutStartSec=4h\n' | \
  sudo tee '/etc/systemd/system/msu-parser@students.service.d/timeout.conf'
sudo systemctl daemon-reload
```

Шаблон не умеет принимать `--resume` — добор запускать разовым юнитом:

```bash
sudo systemd-run --unit=msu-students-resume --collect \
  --property=User=msu --property=Group=msu \
  --property=WorkingDirectory=/opt/msu-schedule \
  --setenv=TZ=Europe/Moscow \
  /opt/msu-schedule/.venv/bin/python run_parser.py students --resume
```

`--resume` на `parse_log` не смотрит: он пропускает группы, у которых уже
собраны предметы. Поэтому обрыв без записи в журнале его не ломает.

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
sudo -u msu .venv/bin/python run_parser.py subgroups
sudo -u msu .venv/bin/python run_parser.py teachers
sudo -u msu .venv/bin/python run_parser.py students
```

Скрипт кладёт резервную копию базы рядом и по умолчанию ничего не удаляет.
Привязки пользователей к студентам при `--students` сбрасываются — людям
придётся заново найти себя по фамилии.

Выбранные языковые потоки переживают смену семестра сами: они хранятся
не номером подгруппы, а связкой «предмет → преподаватель → поток», и каждый
уровень при устаревании отбрасывается отдельно. В худшем случае человек
снова увидит все потоки — это поведение по умолчанию, а не ошибка.

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
