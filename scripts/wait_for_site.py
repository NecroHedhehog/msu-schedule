#!/usr/bin/env python3
"""
Ждать, пока поднимется сайт расписания, и запустить прогон, как только он ответит.

Сайт соцфака регулярно ложится, и сидеть проверять вручную — не дело.
Скрипт дёргает главную с заданным интервалом и запускает run_parser.py,
когда она ответит подряд несколько раз (одиночный успех может быть
случайным — сайт часто «моргает», и стартовать сорокаминутный прогон
в такой момент бессмысленно).

Живым считается не любой ответ 200, а страница, на которой есть ссылки
на отделения (?f=N) — то есть ровно то, с чего начинает парсер. Заглушка
провайдера или страница-ошибка с кодом 200 за живой сайт не сойдут.

Примеры:
    python scripts/wait_for_site.py --check-only
        одна проверка и выход: жив сайт или нет

    python scripts/wait_for_site.py --then socio
        ждать и, как поднимется, собрать групповые расписания

    python scripts/wait_for_site.py --interval 600 --then students --resume
        проверять раз в 10 минут, потом продолжить прогон студентов

Ctrl+C прерывает ожидание в любой момент.
"""

import argparse
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests

from parsers.socio import SocioParser

# Признак живой главной: ссылки на отделения, с которых начинает парсер
ALIVE_MARKER = re.compile(r'\?f=\d+')

MIN_INTERVAL = 30
PROBE_TIMEOUT = 20


def now() -> str:
    return datetime.now().strftime('%H:%M:%S')


def probe(url: str, encoding: str) -> tuple:
    """(жив, что именно ответило) — одна проверка без ретраев."""
    try:
        resp = requests.get(url, timeout=PROBE_TIMEOUT, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36'
        })
    except requests.RequestException as e:
        return False, f"{type(e).__name__}"

    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"

    resp.encoding = encoding
    body = resp.text
    if not body.strip():
        return False, "пустой ответ"
    if not ALIVE_MARKER.search(body):
        return False, f"200, но главная не похожа на себя ({len(body)} байт)"

    return True, f"OK, {len(body)} байт"


def notify(text: str):
    try:
        from core.alerts import send_admin_alert
        send_admin_alert(text)
    except Exception as e:
        print(f"  (уведомление не ушло: {e})")


def run_parser(args: list) -> int:
    cmd = [sys.executable, str(ROOT / 'run_parser.py')] + args
    print(f"\n[{now()}] Запускаю: run_parser.py {' '.join(args)}\n" + "=" * 60)
    completed = subprocess.run(cmd, cwd=str(ROOT))
    print("=" * 60)
    print(f"[{now()}] Прогон завершён, код возврата: {completed.returncode}")
    return completed.returncode


def main():
    ap = argparse.ArgumentParser(
        description='Ждать, пока поднимется сайт расписания.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', default=None,
                    help='что дёргать (по умолчанию главная соцфака)')
    ap.add_argument('--interval', type=int, default=300,
                    help=f'секунд между проверками (минимум {MIN_INTERVAL}, по умолчанию 300)')
    ap.add_argument('--max-hours', type=float, default=12,
                    help='сколько ждать максимум; 0 — без ограничения (по умолчанию 12)')
    ap.add_argument('--stable', type=int, default=2,
                    help='сколько успешных проверок подряд считать за «поднялся» (по умолчанию 2)')
    ap.add_argument('--check-only', action='store_true',
                    help='одна проверка и выход')
    ap.add_argument('--notify', action='store_true',
                    help='написать в Telegram, когда сайт поднимется (нужны BOT_TOKEN и ADMIN_CHAT_ID)')
    ap.add_argument('--then', nargs=argparse.REMAINDER, default=[],
                    help='всё, что после этого флага, уходит в run_parser.py')
    args = ap.parse_args()

    url = args.url or (SocioParser.DOMAIN + '/index.php')
    encoding = SocioParser.ENCODING
    interval = max(MIN_INTERVAL, args.interval)

    # --- одна проверка ---
    if args.check_only:
        alive, reason = probe(url, encoding)
        print(f"[{now()}] {url}")
        print(f"           {'ЖИВ' if alive else 'НЕ ОТВЕЧАЕТ'} — {reason}")
        return 0 if alive else 1

    deadline = None
    if args.max_hours > 0:
        deadline = datetime.now() + timedelta(hours=args.max_hours)

    print(f"[{now()}] Жду {url}")
    print(f"           проверка раз в {interval} с, "
          f"нужно {args.stable} успешных подряд, "
          f"{'без ограничения по времени' if not deadline else 'до ' + deadline.strftime('%H:%M')}")
    if args.then:
        print(f"           как поднимется: run_parser.py {' '.join(args.then)}")
    else:
        print("           прогон не задан (--then), просто сообщу и выйду")

    streak = 0
    attempt = 0

    try:
        while True:
            attempt += 1
            alive, reason = probe(url, encoding)

            if alive:
                streak += 1
                print(f"[{now()}] проверка {attempt}: жив ({streak}/{args.stable}) — {reason}")
                if streak >= args.stable:
                    break
                # Подряд идущие проверки делаем частыми: подтверждаем, а не ждём
                time.sleep(min(30, interval))
                continue

            streak = 0
            print(f"[{now()}] проверка {attempt}: лежит — {reason}")

            if deadline and datetime.now() >= deadline:
                print(f"\n[{now()}] Время вышло, сайт так и не поднялся.")
                return 2

            # Джиттер, чтобы не долбить строго по расписанию
            pause = interval + random.uniform(0, interval * 0.2)
            time.sleep(pause)

    except KeyboardInterrupt:
        print(f"\n[{now()}] Прервано вручную.")
        return 130

    print(f"\n[{now()}] Сайт поднялся (проверок: {attempt}).")

    if args.notify:
        notify(f"🟢 <b>Сайт расписания поднялся</b>\nПроверок заняло: {attempt}")

    if not args.then:
        return 0

    return run_parser(args.then)


if __name__ == '__main__':
    sys.exit(main())
