"""Базовый класс парсера расписания."""

import time
import requests
from abc import ABC, abstractmethod
from core.config import PARSER_REQUEST_DELAY


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

    def download(self, path: str, encoding: str = None) -> str:
        url = self.DOMAIN + path
        print(f"  GET {url}")
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            if encoding:
                resp.encoding = encoding
            time.sleep(PARSER_REQUEST_DELAY)
            return resp.text
        except requests.RequestException as e:
            print(f"  ERROR: {url} — {e}")
            return ''

    @abstractmethod
    def parse(self) -> dict:
        """Вернуть {'groups': [{'code', 'site_id', 'department', 'program', 'lessons': [...]}]}"""
        pass
