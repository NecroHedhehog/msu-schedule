"""
Парсер расписания социологического факультета МГУ.

Два режима:
  parse()          — групповые расписания (быстро, ~100 запросов)
  parse_students() — персональные расписания + преподаватели + списки (2500 запросов, ~33 мин)
"""

import re
from datetime import datetime, date
from bs4 import BeautifulSoup

from parsers.base import BaseParser
from core.config import PAIR_TIMES, PAIR_TIMES_WED_MFK


class SocioParser(BaseParser):
    FACULTY_CODE = 'socio'
    FACULTY_NAME = 'Социологический факультет'
    DOMAIN = 'http://cacs.socio.msu.ru'

    ENCODING = 'cp1251'

    DEPT_LINK_RE = re.compile(r'\?f=\d+')
    PROG_LINK_RE = re.compile(r'\?sp=\d+')
    GROUP_LINK_RE = re.compile(r'\?gr=\d+')
    SELST_LINK_RE = re.compile(r'selst=(\d+)')
    PRR_LINK_RE = re.compile(r"prr=(\d+)")
    CHAIR_LINK_RE = re.compile(r'\?k=\d+')
    TITLE_GROUP_RE = re.compile(r"'\s+у\s+(.+)$")
    DATE_RE = re.compile(r'\d{2}\.\d{2}\.\d{4}')
    TITLE_RE = re.compile(r"(.+?) по '(.+)'")
    PAIR_CELL_CLASS = 'TmTblC'
    LESSON_ID = 'LESS'
    ABBR_COLOR = '#004000'
    TYPE_KEYWORDS = ('Лк', 'Сем', 'Зч', 'Экз', 'Пр', 'Конс', 'Доп')

    SKIP_DEPARTMENTS = {'Администрация'}

    # ======= Режим 1: Групповые расписания (существующий) =======

    def parse(self) -> dict:
        print(f"\n[socio] Парсинг групповых расписаний: {self.DOMAIN}")

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

    # ======= Режим 2: Студенты + преподаватели =======

    def parse_students(self, groups_info: list) -> dict:
        """
        Парсинг персональных расписаний.
        groups_info: [(group_id, code, site_id), ...] из БД.
        Возвращает {'students': [...], 'teacher_updates': [...]}.
        """
        print(f"\n[socio] Парсинг студентов и преподавателей")

        # Заходим в режим "Расписание студента"
        self.download('/index.php?mnu=75', encoding=self.ENCODING)

        all_students = []
        teacher_updates = []  # (group_id, date, pair, subject, teacher)

        for group_id, group_code, site_id in groups_info:
            print(f"\n  [{group_code}] (gr={site_id})")

            # Загружаем страницу группы в режиме студента → список студентов
            group_page = self.download(f'/index.php?gr={site_id}', encoding=self.ENCODING)
            if not group_page:
                continue

            students = self._find_students(group_page)
            if not students:
                print(f"    студентов не найдено, пробуем через mnu=75")
                # Повторно заходим в студенческий режим
                self.download('/index.php?mnu=75', encoding=self.ENCODING)
                group_page = self.download(f'/index.php?gr={site_id}', encoding=self.ENCODING)
                if group_page:
                    students = self._find_students(group_page)

            if not students:
                print(f"    студентов не найдено, пропускаю")
                continue

            print(f"    студентов: {len(students)}")

            # Сохраняем список студентов
            for s in students:
                s['group_id'] = group_id
                s['group_code'] = group_code
            all_students.extend(students)

            # Парсим расписание каждого студента
            seen_teachers = {}  # (date, pair, subject) → teacher
            for i, student in enumerate(students):
                selst_id = student['site_id']
                short = student['short_name']

                # Выбираем студента (сессия)
                self.download(f'/index.php?selst={selst_id}', encoding=self.ENCODING)

                # Потом загружаем месяцы
                lessons = []
                for offset in [0, 1]:
                    url = self._month_url_bare(offset)
                    page = self.download(url, encoding=self.ENCODING)
                    if page:
                        lessons.extend(self._parse_page(page))
                        
                        # Сохраняем предметы этого студента
                student_subjects = list(set(l['subject'] for l in lessons))
                student['subjects'] = student_subjects

                # Собираем преподавателей
                new_teachers = 0
                for l in lessons:
                    if l['teacher']:
                        key = (l['date'], l['pair_number'], l['subject'])
                        if key not in seen_teachers:
                            seen_teachers[key] = l['teacher']
                            new_teachers += 1
                            teacher_updates.append({
                                'group_id': group_id,
                                'date': l['date'],
                                'pair_number': l['pair_number'],
                                'subject': l['subject'],
                                'teacher': l['teacher'],
                            })

                if (i + 1) % 5 == 0 or i == len(students) - 1:
                    print(f"    {i+1}/{len(students)} студентов, "
                          f"преподавателей найдено: {len(seen_teachers)}")

        print(f"\n[socio] Итого: {len(all_students)} студентов, "
              f"{len(teacher_updates)} связок преподаватель-занятие")

        return {
            'students': all_students,
            'teacher_updates': teacher_updates,
        }

    # ======= Режим 3: Преподаватели через кафедры =======

    def parse_teachers(self, group_code_to_id: dict) -> dict:
        """
        Парсинг расписаний преподавателей через кафедры.
        group_code_to_id: {'с203': 5, 'с401': 12, ...} — маппинг кода группы на ID в БД.
        Возвращает {'teacher_updates': [...], 'teachers_found': int}.
        """
        print(f"\n[socio] Парсинг преподавателей через кафедры")

        # Заходим в режим расписания преподавателей
        page = self.download('/index.php?mnu=56', encoding=self.ENCODING)
        if not page:
            return {'teacher_updates': [], 'teachers_found': 0}

        # Собираем кафедры
        chairs = self._find_chairs(page)
        print(f"[socio] Кафедр: {len(chairs)}")

        # Собираем всех преподавателей со всех кафедр
        all_teachers = {}  # prr_id → full_name (дедупликация)
        for chair_name, chair_url in chairs:
            chair_page = self.download(chair_url, encoding=self.ENCODING)
            if not chair_page:
                continue

            teachers = self._find_teachers_on_page(chair_page)
            new = 0
            for prr_id, full_name, short_name in teachers:
                if prr_id not in all_teachers:
                    all_teachers[prr_id] = (full_name, short_name)
                    new += 1
            print(f"  {chair_name}: {len(teachers)} преподов ({new} новых)")

        print(f"[socio] Уникальных преподавателей: {len(all_teachers)}")

        # Парсим расписание каждого преподавателя
        teacher_updates = []

        for idx, (prr_id, (full_name, short_name)) in enumerate(all_teachers.items()):
            # Выбираем преподавателя (сессия)
            self.download(f'/index.php?prr={prr_id}', encoding=self.ENCODING)

            # Загружаем 2 месяца
            for offset in [0, 1]:
                url = self._month_url_bare(offset)
                page = self.download(url, encoding=self.ENCODING)
                if not page:
                    continue

                lessons = self._parse_teacher_page(page, short_name)
                for l in lessons:
                    # Сопоставляем код группы с ID в базе
                    for group_code in l['group_codes']:
                        group_id = group_code_to_id.get(group_code)
                        if group_id:
                            teacher_updates.append({
                                'group_id': group_id,
                                'date': l['date'],
                                'pair_number': l['pair_number'],
                                'subject': l['subject'],
                                'teacher': short_name,
                            })

            if (idx + 1) % 10 == 0 or idx == len(all_teachers) - 1:
                print(f"  {idx+1}/{len(all_teachers)} преподов, обновлений: {len(teacher_updates)}")

        print(f"\n[socio] Итого: {len(all_teachers)} преподавателей, "
              f"{len(teacher_updates)} обновлений занятий")

        return {
            'teacher_updates': teacher_updates,
            'teachers_found': len(all_teachers),
        }

    def _find_chairs(self, html: str) -> list:
        """Найти кафедры на странице преподавателей."""
        soup = BeautifulSoup(html, 'html.parser')
        result = []
        for a in soup.find_all('a', href=self.CHAIR_LINK_RE):
            name = a.get('title') or a.get_text(strip=True)
            href = '/index.php' + a['href']
            if name:
                result.append((name, href))
        return result

    def _find_teachers_on_page(self, html: str) -> list:
        """Найти преподавателей на странице кафедры. Возвращает [(prr_id, full_name, short_name), ...]."""
        soup = BeautifulSoup(html, 'html.parser')
        result = []
        for tr in soup.find_all('tr', onclick=self.PRR_LINK_RE):
            onclick = tr.get('onclick', '')
            m = self.PRR_LINK_RE.search(onclick)
            if not m:
                continue
            prr_id = m.group(1)

            tds = tr.find_all('td')
            if not tds:
                continue

            name_td = tds[0]
            full_name = name_td.get('title', '').strip().rstrip(', ')
            short_name = name_td.get_text(strip=True)
            # Убираем [] из короткого имени
            short_name = re.sub(r'\s*\[.*?\]\s*', '', short_name).strip()

            if full_name and short_name:
                result.append((prr_id, full_name, short_name))

        return result

    def _parse_teacher_page(self, html: str, teacher_name: str) -> list:
        """Парсить расписание преподавателя. Извлекает предмет + группы."""
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

                for div in cell.find_all('div', id=self.LESSON_ID):
                    title = div.get('title', '')

                    # Извлекаем предмет
                    m = self.TITLE_RE.match(title)
                    if not m:
                        continue
                    subject = m.group(2)

                    # Извлекаем группы из " у с203" или " у с301,с302"
                    group_codes = []
                    gm = self.TITLE_GROUP_RE.search(title)
                    if gm:
                        raw_groups = gm.group(1).strip()
                        # "с301,с302,с303" → ['с301', 'с302', 'с303']
                        group_codes = [g.strip() for g in raw_groups.split(',') if g.strip()]

                    if group_codes:
                        lessons.append({
                            'date': iso_date,
                            'pair_number': pair,
                            'subject': subject,
                            'group_codes': group_codes,
                        })

        return lessons

    def _month_url_bare(self, offset=0):
        """URL только с месяцем (преподаватель/студент уже выбран в сессии)."""
        today = date.today()
        m = today.month + offset
        y = today.year
        if m > 12:
            m -= 12
            y += 1
        return f'/index.php?pMns={m}.{y}'

    def _find_students(self, html: str) -> list:
        """Извлечь список студентов из боковой панели."""
        soup = BeautifulSoup(html, 'html.parser')
        students = []

        for tr in soup.find_all('tr', onclick=self.SELST_LINK_RE):
            onclick = tr.get('onclick', '')
            m = self.SELST_LINK_RE.search(onclick)
            if not m:
                continue

            selst_id = m.group(1)

            # Полное ФИО в title второго td
            tds = tr.find_all('td')
            if len(tds) < 2:
                continue

            name_td = tds[1]
            full_name = name_td.get('title', '').strip()
            short_name = name_td.get_text(strip=True)

            if full_name:
                students.append({
                    'site_id': selst_id,
                    'full_name': full_name,
                    'short_name': short_name,
                })

        return students

    # ======= Навигация =======

    def _find_links(self, html, pattern, strip_brackets=False):
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

    # ======= Парсинг расписания =======

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

            # Среда? МФК пары 4-5 имеют другое время
            is_wednesday = False
            try:
                is_wednesday = datetime.strptime(iso_date, '%Y-%m-%d').weekday() == 2
            except ValueError:
                pass

            for i, cell in enumerate(table.find_all('td', class_=self.PAIR_CELL_CLASS)):
                pair = i + 1
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

        # аббревиатура
        abbr = ''
        font = div.find('font', color=self.ABBR_COLOR)
        if font:
            b = font.find('b')
            if b:
                abbr = b.get_text(strip=True)

        # аудитория
        room = ''
        for b in div.find_all('b'):
            t = b.get_text(strip=True)
            if t and t != abbr and not b.find_parent('font', color=self.ABBR_COLOR):
                room = t
                break

        # тип
        type_short = ''
        for f in div.find_all('font'):
            t = f.get_text(strip=True)
            if t in self.TYPE_KEYWORDS:
                type_short = t
                break

        # преподаватель — текст после последнего "]" в содержимом div
        teacher = ''
        full_text = div.get_text(separator='\n')
        idx = full_text.rfind(']')
        if idx >= 0:
            raw_teacher = full_text[idx + 1:].strip()
            # Убираем пустые строки и лишние пробелы, соединяем через ", "
            parts = [p.strip() for p in raw_teacher.split('\n') if p.strip()]
            # Фильтруем: имя преподавателя содержит точку (И.О.) или кириллицу + пробел
            teacher_parts = []
            for p in parts:
                # Пропускаем коды групп (с201, пп301...) и числа
                if re.match(r'^[сСпПмМ\d]', p) and not re.search(r'[А-Я]\.\s*[А-Я]\.', p):
                    continue
                if re.search(r'групп', p):
                    continue
                teacher_parts.append(p)
            teacher = ', '.join(teacher_parts)

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
            'teacher': teacher,
        }
