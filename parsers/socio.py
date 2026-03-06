"""
Парсер расписания Социологического факультета МГУ.
Источник: http://cacs.socio.msu.ru

Навигация по сайту (4 уровня):
  1. Отделение:   ?f=1 (Бакалавриат), ?f=2 (Магистратура)
  2. Направление:  ?sp=1 (Социология), ?sp=5 (ППиСН), ?sp=2 (Менеджмент)...
  3. Год набора:   select name=yr → value=42 (2025), value=39 (2024)...
  4. Группа:       ?gr=279 (с403)

Структура HTML расписания:
  - Каждый день — <table> с датой в заголовке
  - 6 строк = 6 пар (ячейки с class="TmTblC")
  - Занятие — <div id="LESS"> с title="Лекция по 'Предмет'"
  - Внутри: аббревиатура (зелёный bold), аудитория (bold), тип [Лк/Сем/Зч]
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

    def parse(self) -> dict:
        """Парсит расписание всех групп соцфака."""
        print(f"\n🎓 Парсинг: {self.FACULTY_NAME}")
        print(f"   Домен: {self.DOMAIN}\n")

        # 1. Загружаем главную, находим отделения
        main_page = self.download('/index.php', encoding='cp1251')
        if not main_page:
            return {'groups': []}

        departments = self._find_departments(main_page)
        print(f"  📋 Найдено отделений: {len(departments)}")

        all_groups = []

        # 2. Для каждого отделения → направления → годы → группы
        for dept_name, dept_url in departments:
            print(f"\n  📂 {dept_name}")
            dept_page = self.download(dept_url, encoding='cp1251')
            if not dept_page:
                continue

            # Найти направления (sp=)
            programs = self._find_programs(dept_page)
            if not programs:
                # Если направлений нет, считаем что группы прямо тут
                programs = [('', None)]

            print(f"     Направлений: {len(programs)}")

            for prog_name, prog_url in programs:
                if prog_name:
                    print(f"\n     📑 Направление: {prog_name}")

                if prog_url:
                    prog_page = self.download(prog_url, encoding='cp1251')
                else:
                    prog_page = dept_page
                if not prog_page:
                    continue

                # Найти годы набора
                years = self._find_years(prog_page)
                if not years:
                    years = [('', None)]

                for year_label, year_url in years:
                    if year_label:
                        print(f"        📅 Год набора: {year_label}")

                    if year_url:
                        year_page = self.download(year_url, encoding='cp1251')
                    else:
                        year_page = prog_page
                    if not year_page:
                        continue

                    # Найти группы
                    groups = self._find_groups(year_page)
                    if not groups:
                        continue

                    print(f"           Групп: {len(groups)}")

                    # 3. Для каждой группы парсим расписание
                    for group_code, group_url, site_id in groups:
                        print(f"           👥 {group_code} (id={site_id})", end='')

                        lessons = []
                        for months_offset in [0, 1]:
                            target = self._get_month_url(group_url, months_offset)
                            page = self.download(target, encoding='cp1251')
                            if page:
                                month_lessons = self._parse_schedule_page(page)
                                lessons.extend(month_lessons)

                        print(f" → {len(lessons)} занятий")

                        all_groups.append({
                            'code': group_code,
                            'site_id': site_id,
                            'department': dept_name,
                            'program': prog_name,
                            'lessons': lessons,
                        })

        total_lessons = sum(len(g['lessons']) for g in all_groups)
        print(f"\n✅ Итого: {len(all_groups)} групп, {total_lessons} занятий\n")

        return {'groups': all_groups}

    # === Навигация по сайту ===

    def _find_departments(self, html: str) -> list[tuple[str, str]]:
        """Найти отделения (Бакалавриат, Магистратура)."""
        soup = BeautifulSoup(html, 'html.parser')
        result = []
        for link in soup.find_all('a', href=re.compile(r'\?f=\d+')):
            name = link.get_text(strip=True)
            href = '/index.php' + link['href']
            if name and name != 'Администрация':
                result.append((name, href))
        return result

    def _find_programs(self, html: str) -> list[tuple[str, str]]:
        """Найти направления/программы (Соц, ППиСН, Мен...)."""
        soup = BeautifulSoup(html, 'html.parser')
        result = []
        for link in soup.find_all('a', href=re.compile(r'\?sp=\d+')):
            name = link.get_text(strip=True).strip('[]')
            href = '/index.php' + link['href']
            if name:
                result.append((name, href))
        return result

    def _find_years(self, html: str) -> list[tuple[str, str]]:
        """Найти годы набора из <select name="yr">."""
        soup = BeautifulSoup(html, 'html.parser')
        select = soup.find('select', {'name': 'yr'})
        if not select:
            return []

        result = []
        for option in select.find_all('option'):
            value = option.get('value', '')
            label = option.get_text(strip=True)
            if value and label:
                href = f'/index.php?yr={value}'
                result.append((label, href))
        return result

    def _find_groups(self, html: str) -> list[tuple[str, str, str]]:
        """Найти все группы на странице."""
        soup = BeautifulSoup(html, 'html.parser')
        result = []
        for link in soup.find_all('a', href=re.compile(r'\?gr=\d+')):
            code = link.get_text(strip=True)
            href = '/index.php' + link['href']
            match = re.search(r'gr=(\d+)', link['href'])
            site_id = match.group(1) if match else ''
            if code:
                result.append((code, href, site_id))
        return result

    def _get_month_url(self, group_url: str, months_offset: int = 0) -> str:
        """Добавить параметр месяца к URL группы."""
        today = date.today()
        month = today.month + months_offset
        year = today.year
        if month > 12:
            month -= 12
            year += 1
        return f"{group_url}&pMns={month}.{year}"

    # === Парсинг расписания ===

    def _parse_schedule_page(self, html: str) -> list[dict]:
        """Разобрать страницу расписания, вернуть список занятий."""
        soup = BeautifulSoup(html, 'html.parser')
        lessons = []

        date_cells = soup.find_all(
            'td', string=re.compile(r'\d{2}\.\d{2}\.\d{4}')
        )

        for date_cell in date_cells:
            date_text = date_cell.get_text(strip=True)

            try:
                date_obj = datetime.strptime(date_text, '%d.%m.%Y')
                date_iso = date_obj.strftime('%Y-%m-%d')
            except ValueError:
                continue

            parent_table = date_cell.find_parent('table')
            if not parent_table:
                continue

            pair_cells = parent_table.find_all('td', class_='TmTblC')

            for pair_idx, cell in enumerate(pair_cells):
                pair_num = pair_idx + 1
                time_start, time_end = PAIR_TIMES.get(pair_num, ('?', '?'))

                less_divs = cell.find_all('div', id='LESS')

                for div in less_divs:
                    lesson = self._parse_lesson_div(
                        div, date_iso, pair_num, time_start, time_end
                    )
                    if lesson:
                        lessons.append(lesson)

        return lessons

    def _parse_lesson_div(self, div, date_iso: str, pair_num: int,
                           time_start: str, time_end: str) -> dict | None:
        """Разобрать один div с занятием."""
        title = div.get('title', '')

        title_match = re.match(r"(.+?) по '(.+)'", title)
        if not title_match:
            return None

        type_full = title_match.group(1)
        subject = title_match.group(2)

        # Аббревиатура
        abbr = ''
        abbr_font = div.find('font', color='#004000')
        if abbr_font:
            b_tag = abbr_font.find('b')
            if b_tag:
                abbr = b_tag.get_text(strip=True)

        # Аудитория
        room = ''
        for b_tag in div.find_all('b'):
            text = b_tag.get_text(strip=True)
            if text and text != abbr and not b_tag.find_parent('font', color='#004000'):
                room = text
                break

        # Тип занятия
        type_short = ''
        for font_tag in div.find_all('font'):
            text = font_tag.get_text(strip=True)
            if text in ('Лк', 'Сем', 'Зч', 'Экз', 'Пр', 'Конс'):
                type_short = text
                break

        return {
            'date': date_iso,
            'pair_number': pair_num,
            'time_start': time_start,
            'time_end': time_end,
            'subject': subject,
            'subject_abbr': abbr,
            'lesson_type': type_short,
            'lesson_type_full': type_full,
            'room': room,
            'teacher': '',
        }
