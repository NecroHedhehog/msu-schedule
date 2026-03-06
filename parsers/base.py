"""
Базовый класс парсера расписания.
Все парсеры факультетов наследуются от него.
"""

import time
import requests
from abc import ABC, abstractmethod
from core.config import PARSER_REQUEST_DELAY


class BaseParser(ABC):
    """
    Абстрактный парсер. Каждый факультет реализует свой подкласс.
    """

    # Переопределить в подклассе:
    FACULTY_CODE = ''       # 'socio', 'spa', 'law'
    FACULTY_NAME = ''       # 'Социологический факультет'
    DOMAIN = ''             # 'http://cacs.socio.msu.ru'

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36'
        })

    def download(self, path: str, encoding: str = None) -> str:
        """
        Скачать страницу с сайта.
        Добавляет задержку между запросами, чтобы не нагружать сервер.
        """
        url = self.DOMAIN + path
        print(f"  📥 {url}")

        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()

            if encoding:
                response.encoding = encoding

            time.sleep(PARSER_REQUEST_DELAY)
            return response.text

        except requests.RequestException as e:
            print(f"  ❌ Ошибка загрузки {url}: {e}")
            return ''

    @abstractmethod
    def parse(self) -> dict:
        """
        Запустить парсинг. Должен вернуть словарь:
        {
            'groups': [
                {
                    'code': 'с403',
                    'site_id': '279',
                    'department': 'Бакалавриат',
                    'program': 'Социология',
                    'lessons': [
                        {
                            'date': '2026-03-05',
                            'pair_number': 3,
                            'time_start': '12:55',
                            'time_end': '14:25',
                            'subject': 'Социология морали',
                            'subject_abbr': 'СоцМ',
                            'lesson_type': 'Лк',
                            'lesson_type_full': 'Лекция',
                            'room': '415',
                            'teacher': '',
                        },
                        ...
                    ]
                },
                ...
            ]
        }
        """
        pass
