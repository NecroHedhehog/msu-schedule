"""Базовый класс парсера расписания."""

import random
import time
import requests
from abc import ABC, abstractmethod

from core.config import (
    PARSER_REQUEST_DELAY,
    PARSER_REQUEST_TIMEOUT,
    PARSER_MAX_ATTEMPTS,
    PARSER_RETRY_BACKOFF,
)


class FetchError(Exception):
    """Обязательная страница не скачалась после всех попыток."""

    def __init__(self, url: str, reason: str):
        self.url = url
        self.reason = reason
        super().__init__(f"{url} — {reason}")


# Коды, при которых имеет смысл повторить запрос.
# 404/403 повторять бессмысленно — страницы просто нет.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Сколько неудачных URL держим в памяти для отчёта
MAX_REMEMBERED_FAILURES = 50


class BaseParser(ABC):

    FACULTY_CODE = ''
    FACULTY_NAME = ''
    DOMAIN = ''

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            )
        })
        # Счётчики прогона: парсер обязан уметь рассказать, чего он не смог
        self.requests_ok = 0
        self.requests_failed = 0
        self.retries_used = 0
        self.failed_urls = []
        self.unparsed_blocks = 0
        self.unparsed_samples = []
        self.repaired_titles = 0
        # Дни, где номер пары не удалось прочитать из разметки и пришлось
        # считать по позиции ячейки. Позиция врёт, когда сайт выбрасывает
        # пустые верхние строки, поэтому такие дни надо видеть.
        self.days_without_ruler = 0
        self.ruler_samples = []

    # ======= Статистика прогона =======

    def stats(self) -> dict:
        return {
            'requests_ok': self.requests_ok,
            'requests_failed': self.requests_failed,
            'retries_used': self.retries_used,
            'unparsed_blocks': self.unparsed_blocks,
            'repaired_titles': self.repaired_titles,
            'days_without_ruler': self.days_without_ruler,
            'failed_urls': list(self.failed_urls),
        }

    def stats_line(self) -> str:
        return (
            f"запросов ок: {self.requests_ok}, "
            f"провалено: {self.requests_failed}, "
            f"повторов: {self.retries_used}, "
            f"нераспознанных блоков: {self.unparsed_blocks}, "
            f"починено заголовков: {self.repaired_titles}, "
            f"дней без линейки пар: {self.days_without_ruler}"
        )

    def _note_missing_ruler(self, date_hint: str, cells: int):
        """
        День, у которого не нашлось колонки с номерами пар.

        Номер пришлось взять по позиции ячейки, а это верно только когда
        сайт рисует все строки от первой пары. Если он выбросил пустые
        верхние — весь день уедет вверх, и молча.
        """
        self.days_without_ruler += 1
        if len(self.ruler_samples) < 10:
            self.ruler_samples.append(f"{date_hint}: {cells} ячеек")

    def _note_unparsed(self, title: str, date_hint: str = '', pair_hint=''):
        """Блок занятия, который парсер не понял. Раньше такие терялись молча."""
        self.unparsed_blocks += 1
        if len(self.unparsed_samples) < 10:
            self.unparsed_samples.append(f"{date_hint} п.{pair_hint}: {title[:120]!r}")

    def _note_failure(self, url: str, reason: str):
        self.requests_failed += 1
        if len(self.failed_urls) < MAX_REMEMBERED_FAILURES:
            self.failed_urls.append(f"{url} — {reason}")

    def _polite_delay(self):
        if PARSER_REQUEST_DELAY > 0:
            time.sleep(PARSER_REQUEST_DELAY)

    # ======= Загрузка =======

    def download(self, path: str, encoding: str = None,
                 required: bool = False, attempts: int = None) -> str:
        """
        Скачать страницу с повторами и экспоненциальным бэкоффом.

        required=True — страница критична для навигации; если не скачалась,
        бросаем FetchError вместо тихого '' (иначе парсер пойдёт дальше
        и запишет в базу пустоту, отрапортовав 'ok').
        """
        url = self.DOMAIN + path
        attempts = attempts or PARSER_MAX_ATTEMPTS
        print(f"  GET {url}")

        last_reason = 'неизвестно'

        for attempt in range(1, attempts + 1):
            retryable = True
            try:
                resp = self.session.get(url, timeout=PARSER_REQUEST_TIMEOUT)
            except requests.RequestException as e:
                last_reason = f"{type(e).__name__}: {e}"
            else:
                status = resp.status_code
                if status >= 400 and status not in RETRY_STATUSES:
                    # 404, 403 и подобное — повторять нечего
                    last_reason = f"HTTP {status}"
                    retryable = False
                elif status in RETRY_STATUSES:
                    last_reason = f"HTTP {status}"
                else:
                    if encoding:
                        resp.encoding = encoding
                    text = resp.text
                    if text.strip():
                        self.requests_ok += 1
                        self._polite_delay()
                        return text
                    last_reason = 'пустой ответ'

            if retryable and attempt < attempts:
                self.retries_used += 1
                pause = PARSER_RETRY_BACKOFF * (2 ** (attempt - 1))
                pause += random.uniform(0, pause * 0.3)
                print(f"    retry {attempt}/{attempts - 1}: {last_reason}, жду {pause:.1f}с")
                time.sleep(pause)
                continue

            # Попытки кончились либо повторять бессмысленно
            self._note_failure(url, last_reason)
            print(f"    FAIL: {last_reason}")
            self._polite_delay()
            if required:
                raise FetchError(url, last_reason)
            return ''

        return ''

    @abstractmethod
    def parse(self) -> dict:
        """Вернуть {'groups': [{'code', 'site_id', 'department', 'program', 'lessons': [...]}]}"""
        pass
