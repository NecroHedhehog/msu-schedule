"""
Парсер расписания социологического факультета МГУ.

Навигация: отделение → направление → год набора → группа.
Расписание отдаётся помесячно, по неделям внутри дня.
"""

import re
from datetime import datetime, date
from bs4 import BeautifulSoup

from parsers.base import BaseParser
from core.config import PAIR_TIMES


class SocioParser(BaseParser):
    FACULTY_CODE = 'socio'
    FACULTY_NAME = 'Социологический факультет'
    DOMAIN = 'http://cacs.socio.msu.ru'

    ENCODING = 'cp1251'

    # CSS-селекторы и паттерны — специфичны для cacs.socio
    DEPT_LINK_RE = re.compile(r'\?f=\d+')
    PROG_LINK_RE = re.compile(r'\?sp=\d+')
    GROUP_LINK_RE = re.compile(r'\?gr=\d+')
    DATE_RE = re.compile(r'\d{2}\.\d{2}\.\d{4}')
    TITLE_RE = re.compile(r"(.+?) по '(.+)'")
    PAIR_CELL_CLASS = 'TmTblC'
    LESSON_ID = 'LESS'
    ABBR_COLOR = '#004000'
    TYPE_KEYWORDS = ('Лк', 'Сем', 'Зч', 'Экз', 'Пр', 'Конс')

    SKIP_DEPARTMENTS = {'Администрация'}

    def parse(self) -> dict:
        print(f"\n[socio] Начинаю парсинг: {self.DOMAIN}")

        main_page = self.download('/index.php', encoding=self.ENCODING)
        if not main_page:
            return {'groups': []}

        departments = self._find_links(main_page, self.DEPT_LINK_RE)
        departments = [(n, u) for n, u in departments if n not in self.SKIP_DEPARTMENTS]
        print(f"[socio] Отделений: {len(departments)}")

        all_groups = []

        for dept_name, dept_url in departments:
            print(f"\n  [{dept_name}]")
            dept_page = self.download(dept_url, encoding=self.ENCODING)
            if not dept_page:
                continue

            programs = self._find_links(dept_page, self.PROG_LINK_RE, strip_brackets=True)
            if not programs:
                programs = [('', None)]

            for prog_name, prog_url in programs:
                if prog_name:
                    print(f"    направление: {prog_name}")
                prog_page = self.download(prog_url, encoding=self.ENCODING) if prog_url else dept_page
                if not prog_page:
                    continue

                years = self._find_years(prog_page)
                if not years:
                    years = [('', None)]

                for year_label, year_url in years:
                    if year_label:
                        print(f"      год: {year_label}")
                    year_page = self.download(year_url, encoding=self.ENCODING) if year_url else prog_page
                    if not year_page:
                        continue

                    groups = self._find_links(year_page, self.GROUP_LINK_RE)
                    if not groups:
                        continue

                    for group_code, group_url in groups:
                        site_id = re.search(r'gr=(\d+)', group_url)
                        site_id = site_id.group(1) if site_id else ''

                        lessons = self._fetch_group_schedule(group_url)
                        print(f"        {group_code}: {len(lessons)} занятий")

                        all_groups.append({
                            'code': group_code,
                            'site_id': site_id,
                            'department': dept_name,
                            'program': prog_name,
                            'lessons': lessons,
                        })

        total = sum(len(g['lessons']) for g in all_groups)
        print(f"\n[socio] Итого: {len(all_groups)} групп, {total} занятий")
        return {'groups': all_groups}

    # -- навигация --

    def _find_links(self, html, pattern, strip_brackets=False):
        """Найти ссылки по regex паттерну href."""
        soup = BeautifulSoup(html, 'html.parser')
        result = []
        for a in soup.find_all('a', href=pattern):
            name = a.get_text(strip=True)
            if strip_brackets:
                name = name.strip('[]')
            if name:
                result.append((name, '/index.php' + a['href']))
        return result

    def _find_years(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        select = soup.find('select', {'name': 'yr'})
        if not select:
            return []
        return [
            (opt.get_text(strip=True), f'/index.php?yr={opt["value"]}')
            for opt in select.find_all('option')
            if opt.get('value') and opt.get_text(strip=True)
        ]

    def _fetch_group_schedule(self, group_url):
        """Загрузить расписание группы за текущий и следующий месяц."""
        lessons = []
        for offset in [0, 1]:
            url = self._month_url(group_url, offset)
            page = self.download(url, encoding=self.ENCODING)
            if page:
                lessons.extend(self._parse_page(page))
        return lessons

    def _month_url(self, group_url, offset=0):
        today = date.today()
        m = today.month + offset
        y = today.year
        if m > 12:
            m -= 12
            y += 1
        return f"{group_url}&pMns={m}.{y}"

    # -- парсинг расписания --

    def _parse_page(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        lessons = []

        for date_cell in soup.find_all('td', string=self.DATE_RE):
            raw_date = date_cell.get_text(strip=True)
            try:
                iso_date = datetime.strptime(raw_date, '%d.%m.%Y').strftime('%Y-%m-%d')
            except ValueError:
                continue

            table = date_cell.find_parent('table')
            if not table:
                continue

            for i, cell in enumerate(table.find_all('td', class_=self.PAIR_CELL_CLASS)):
                pair = i + 1
                 # По средам пары 4-5 — МФК, другое время
                from core.config import PAIR_TIMES_WED_MFK
                is_wednesday = False
                try:
                    is_wednesday = datetime.strptime(iso_date, '%Y-%m-%d').weekday() == 2
                except ValueError:
                    pass
                if is_wednesday and pair in PAIR_TIMES_WED_MFK:
                    t_start, t_end = PAIR_TIMES_WED_MFK[pair]
                else:
                    t_start, t_end = PAIR_TIMES.get(pair, ('?', '?'))

                for div in cell.find_all('div', id=self.LESSON_ID):
                    parsed = self._parse_lesson(div, iso_date, pair, t_start, t_end)
                    if parsed:
                        lessons.append(parsed)

        return lessons

    def _parse_lesson(self, div, iso_date, pair, t_start, t_end):
        title = div.get('title', '')
        m = self.TITLE_RE.match(title)
        if not m:
            return None

        type_full = m.group(1)
        subject = m.group(2)

        # аббревиатура — зелёный bold
        abbr = ''
        font = div.find('font', color=self.ABBR_COLOR)
        if font:
            b = font.find('b')
            if b:
                abbr = b.get_text(strip=True)

        # аудитория — первый bold, не являющийся аббревиатурой
        room = ''
        for b in div.find_all('b'):
            t = b.get_text(strip=True)
            if t and t != abbr and not b.find_parent('font', color=self.ABBR_COLOR):
                room = t
                break

        # тип: Лк, Сем, Зч...
        type_short = ''
        for f in div.find_all('font'):
            t = f.get_text(strip=True)
            if t in self.TYPE_KEYWORDS:
                type_short = t
                break

        return {
            'date': iso_date,
            'pair_number': pair,
            'time_start': t_start,
            'time_end': t_end,
            'subject': subject,
            'subject_abbr': abbr,
            'lesson_type': type_short,
            'lesson_type_full': type_full,
            'room': room,
            'teacher': '',  # TODO: на соцфаке не указан, на ФГУ — есть
        }
