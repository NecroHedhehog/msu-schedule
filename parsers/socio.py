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
from core.config import PAIR_TIMES, PAIR_TIMES_WED_MFK, SKIP_SUBGROUP_SUBJECTS


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
    # Колонка-линейка слева от дня: номер пары подписан временем в title
    # («10.40-12.10»). Время нужно только чтобы отличить номер пары
    # от случайной цифры в ячейке занятия — само оно не берётся, см. §8.
    PAIR_NUM_RE = re.compile(r'^[1-8]$')
    PAIR_TIME_TITLE_RE = re.compile(r'^\d{1,2}[.:]\d{2}\s*-\s*\d{1,2}[.:]\d{2}$')
    ABBR_COLOR = '#004000'
    TYPE_KEYWORDS = ('Лк', 'Сем', 'Зч', 'Экз', 'Пр', 'Конс', 'Доп')

    # Сайт кладёт в title="..." неэкранированные кавычки:
    #   title="Лекция по 'Анализ ... в программе "Статистический пакет..."'"
    # Любой HTML-парсер обрывает атрибут на первой внутренней кавычке, и
    # занятие теряется молча. Ловим открывающий тег занятия целиком:
    # содержимое title — это либо не-кавычка, либо кавычка, за которой НЕ идёт
    # '>', то есть настоящий конец атрибута определяется по '">'.
    # Сужено до div'ов занятия: у td и a атрибут title не последний, и
    # чинить их той же меркой нельзя.
    LESSON_TITLE_RE = re.compile(
        r'(<div\s+id=["\']?LESS["\']?[^>]*?title=")((?:[^"]|"(?!>))*)(">)',
        re.I | re.S)

    SKIP_DEPARTMENTS = {'Администрация'}

    # ======= Режим 1: Групповые расписания (существующий) =======

    def parse(self, on_group=None) -> dict:
        """
        on_group(group_data) — вызывается сразу после сбора каждой группы.
        Позволяет сохранять по мере продвижения, а не одним куском в конце:
        обрыв на 40-й группе из 46 больше не обнуляет все 40.
        """
        print(f"\n[socio] Парсинг групповых расписаний: {self.DOMAIN}")

        # Главная критична: без неё нет ни одного отделения, и продолжать
        # смысла нет — иначе запишем в базу пустоту и отрапортуем 'ok'.
        main_page = self.download('/index.php', encoding=self.ENCODING, required=True)

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

                        try:
                            lessons = self._fetch_group_schedule(group_url)
                        except Exception as e:
                            # Одна упавшая группа не должна уносить прогон
                            print(f"        {group_code}: ОШИБКА — {e}")
                            self._note_failure(group_url, f"группа {group_code}: {e}")
                            continue

                        print(f"        {group_code}: {len(lessons)} занятий")

                        group_data = {
                            'code': group_code,
                            'site_id': site_id,
                            'department': dept_name,
                            'program': prog_name,
                            'lessons': lessons,
                        }
                        all_groups.append(group_data)

                        if on_group:
                            try:
                                on_group(group_data)
                            except Exception as e:
                                print(f"        {group_code}: НЕ СОХРАНЕНО — {e}")
                                self._note_failure(group_url, f"сохранение {group_code}: {e}")

        total = sum(len(g['lessons']) for g in all_groups)
        print(f"\n[socio] Итого: {len(all_groups)} групп, {total} занятий")
        return {'groups': all_groups}

    # ======= Режим 2: Студенты + преподаватели =======

    def parse_students(self, groups_info: list, on_group=None, skip_group=None) -> dict:
        """
        Парсинг персональных расписаний.
        groups_info: [(group_id, code, site_id), ...] из БД.

        on_group(group_id, students, teacher_updates) — вызывается после каждой
        группы: сохраняем по ходу, а не 3349 запросов спустя.
        skip_group(group_id, code) -> bool — пропустить уже собранную группу,
        чтобы возобновить прогон после обрыва, а не начинать заново.

        Возвращает {'students': [...], 'teacher_updates': [...]}.
        """
        print(f"\n[socio] Парсинг студентов и преподавателей")

        # Режим "Расписание студента" критичен: без него страницы групп
        # приходят без списков, и прогон молча соберёт ноль студентов.
        self.download('/index.php?mnu=75', encoding=self.ENCODING, required=True)

        all_students = []
        teacher_updates = []
        skipped = 0

        for group_id, group_code, site_id in groups_info:
            if skip_group and skip_group(group_id, group_code):
                skipped += 1
                print(f"\n  [{group_code}] уже собрана, пропускаю")
                continue

            print(f"\n  [{group_code}] (gr={site_id})")

            try:
                students, group_updates = self._parse_group_students(
                    group_id, group_code, site_id)
            except Exception as e:
                # Упавшая группа не уносит прогон целиком
                print(f"    ОШИБКА группы {group_code}: {e}")
                self._note_failure(f'/index.php?gr={site_id}', f"группа {group_code}: {e}")
                continue

            if not students:
                continue

            all_students.extend(students)
            teacher_updates.extend(group_updates)

            if on_group:
                try:
                    on_group(group_id, students, group_updates)
                except Exception as e:
                    print(f"    {group_code}: НЕ СОХРАНЕНО — {e}")
                    self._note_failure(f'/index.php?gr={site_id}',
                                       f"сохранение {group_code}: {e}")

        if skipped:
            print(f"\n[socio] Пропущено уже собранных групп: {skipped}")

        print(f"\n[socio] Итого: {len(all_students)} студентов, "
              f"{len(teacher_updates)} связок преподаватель-занятие")

        return {
            'students': all_students,
            'teacher_updates': teacher_updates,
        }

    def _parse_group_students(self, group_id, group_code, site_id):
        """Студенты одной группы: список, их предметы, связки преподаватель-занятие."""
        group_page = self.download(f'/index.php?gr={site_id}', encoding=self.ENCODING)
        if not group_page:
            return [], []

        students = self._find_students(group_page, group_code)
        if not students:
            print(f"    студентов не найдено, пробуем через mnu=75")
            self.download('/index.php?mnu=75', encoding=self.ENCODING)
            group_page = self.download(f'/index.php?gr={site_id}', encoding=self.ENCODING)
            if group_page:
                students = self._find_students(group_page, group_code)

        if not students:
            print(f"    студентов не найдено, пропускаю")
            return [], []

        print(f"    студентов: {len(students)}")

        for s in students:
            s['group_id'] = group_id
            s['group_code'] = group_code
            s['subjects'] = []

        teacher_updates = []
        seen_teachers = {}   # (date, pair, subject) -> teacher
        failed = 0

        for i, student in enumerate(students):
            selst_id = student['site_id']

            try:
                # Выбираем студента (сессия), затем два месяца его расписания
                self.download(f'/index.php?selst={selst_id}', encoding=self.ENCODING)

                lessons = []
                for offset in [0, 1]:
                    page = self.download(self._month_url_bare(offset),
                                         encoding=self.ENCODING)
                    if page:
                        lessons.extend(self._parse_page(page))
            except Exception as e:
                failed += 1
                print(f"    студент {student.get('short_name', selst_id)}: ошибка {e}")
                continue

            student['subjects'] = sorted(set(l['subject'] for l in lessons))

            for l in lessons:
                if not l['teacher']:
                    continue
                key = (l['date'], l['pair_number'], l['subject'])
                if key in seen_teachers:
                    continue
                seen_teachers[key] = l['teacher']
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

        if failed:
            print(f"    не удалось собрать расписание у {failed} студентов из {len(students)}")

        return students, teacher_updates

    # ======= Режим 2б: Подгруппы (языковые потоки) =======

    def parse_subgroups(self, groups_info: list, on_subgroup=None) -> dict:
        """
        Расписания подгрупп — потоков иностранного языка.

        Отдельный проход, потому что стоит дёшево: список группы плюс три
        запроса на подгруппу. На 49 групп это около 350 запросов против 3350
        у полного студенческого прохода, а даёт то, чего в групповом
        расписании нет вовсе.

        on_subgroup(group_id, метка, занятия) — вызывается после каждой
        подгруппы, чтобы сохранять по ходу.
        """
        print(f"\n[socio] Парсинг подгрупп (языковые потоки)")

        # Без режима студента список подгрупп не отдаётся
        self.download('/index.php?mnu=75', encoding=self.ENCODING, required=True)

        groups_with_subgroups = 0
        total_subgroups = 0
        total_lessons = 0
        skipped_foreign = 0

        for group_id, group_code, site_id in groups_info:
            group_page = self.download(f'/index.php?gr={site_id}', encoding=self.ENCODING)
            if not group_page:
                continue

            subgroups = self._find_roster(group_page, group_code)['subgroups']
            if not subgroups:
                continue

            groups_with_subgroups += 1
            print(f"\n  [{group_code}] подгрупп: {len(subgroups)}")

            for sg in subgroups:
                try:
                    self.download(f"/index.php?selst={sg['site_id']}",
                                  encoding=self.ENCODING)
                    lessons = []
                    for offset in [0, 1]:
                        page = self.download(self._month_url_bare(offset),
                                             encoding=self.ENCODING)
                        if page:
                            lessons.extend(self._parse_page(page))
                except Exception as e:
                    print(f"    {sg['label']}: ошибка — {e}")
                    self._note_failure(f"/index.php?selst={sg['site_id']}",
                                       f"подгруппа {sg['label']}: {e}")
                    continue

                foreign = self.foreign_stream_subject(lessons)
                if foreign:
                    skipped_foreign += 1
                    print(f"    {sg['label']}: пропущен — «{foreign[:40]}», "
                          f"поток для иностранных студентов")
                    continue

                total_subgroups += 1
                total_lessons += len(lessons)

                saved = None
                if on_subgroup:
                    try:
                        saved = on_subgroup(group_id, sg['label'], lessons)
                    except Exception as e:
                        print(f"    {sg['label']}: НЕ СОХРАНЕНО — {e}")
                        self._note_failure(f"/index.php?selst={sg['site_id']}",
                                           f"сохранение {sg['label']}: {e}")

                extra = f", своих {saved}" if saved is not None else ""
                print(f"    {sg['label']}: {len(lessons)} занятий на странице{extra}")

        print(f"\n[socio] Итого: {groups_with_subgroups} групп с подгруппами, "
              f"{total_subgroups} подгрупп, {total_lessons} занятий на страницах")
        if skipped_foreign:
            print(f"[socio] Пропущено потоков для иностранных студентов: {skipped_foreign}")

        return {
            'groups_with_subgroups': groups_with_subgroups,
            'subgroups': total_subgroups,
            'lessons_seen': total_lessons,
            'skipped_foreign': skipped_foreign,
        }

    # ======= Режим 3: Преподаватели через кафедры =======

    def parse_teachers(self, group_code_to_id: dict, on_teacher=None) -> dict:
        """
        Парсинг расписаний преподавателей через кафедры.
        group_code_to_id: {'с203': 5, 'с401': 12, ...} — маппинг кода группы на ID в БД.

        on_teacher(updates) — вызывается после каждого преподавателя,
        чтобы обрыв на 500-м из 586 не обнулял предыдущих.

        Возвращает {'teacher_updates': [...], 'teachers_found': int}.
        """
        print(f"\n[socio] Парсинг преподавателей через кафедры")

        # Режим расписания преподавателей критичен: без него нет кафедр.
        page = self.download('/index.php?mnu=56', encoding=self.ENCODING, required=True)

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

        failed = 0

        for idx, (prr_id, (full_name, short_name)) in enumerate(all_teachers.items()):
            own_updates = []
            try:
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
                                own_updates.append({
                                    'group_id': group_id,
                                    'date': l['date'],
                                    'pair_number': l['pair_number'],
                                    'subject': l['subject'],
                                    'teacher': short_name,
                                })
            except Exception as e:
                failed += 1
                print(f"  преподаватель {short_name}: ошибка — {e}")
                self._note_failure(f'/index.php?prr={prr_id}', f"{short_name}: {e}")
                continue

            teacher_updates.extend(own_updates)

            if on_teacher and own_updates:
                try:
                    on_teacher(own_updates)
                except Exception as e:
                    print(f"  {short_name}: НЕ СОХРАНЕНО — {e}")
                    self._note_failure(f'/index.php?prr={prr_id}',
                                       f"сохранение {short_name}: {e}")

            if (idx + 1) % 10 == 0 or idx == len(all_teachers) - 1:
                print(f"  {idx+1}/{len(all_teachers)} преподов, обновлений: {len(teacher_updates)}")

        if failed:
            print(f"[socio] Не удалось обработать преподавателей: {failed}")

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
        soup = BeautifulSoup(self._repair_lesson_titles(html), 'html.parser')
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

            # Номер пары читаем из линейки, как и на странице группы.
            # Иначе смысла в правке нет: имя ищет своё занятие по
            # (группа, дата, НОМЕР ПАРЫ, предмет), и сдвиг на любой
            # из двух сторон рвёт привязку.
            cells = table.find_all('td', class_=self.PAIR_CELL_CLASS)
            numbers = self._pair_numbers(table, len(cells))
            if numbers is None:
                self._note_missing_ruler(raw_date, len(cells))
                numbers = list(range(1, len(cells) + 1))

            for i, cell in enumerate(cells):
                pair = numbers[i]

                for div in cell.find_all('div', id=self.LESSON_ID):
                    title = div.get('title', '')

                    # Извлекаем предмет
                    m = self.TITLE_RE.match(title)
                    if not m:
                        self._note_unparsed(title, iso_date, pair)
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

    @staticmethod
    def foreign_stream_subject(lessons: list) -> str:
        """
        Если поток учит предмет из SKIP_SUBGROUP_SUBJECTS — вернуть его
        название, иначе пустую строку.

        Так отсеиваются потоки для иностранных студентов: «Русский язык
        как иностранный» и подобное. Смотрим на предмет, а не на код группы,
        потому что коды пересобираются каждый семестр, а названия предметов
        живут годами.
        """
        for l in lessons:
            subject = (l.get('subject') or '') if isinstance(l, dict) else (l['subject'] or '')
            for marker in SKIP_SUBGROUP_SUBJECTS:
                if marker.lower() in subject.lower():
                    return subject
        return ''

    @staticmethod
    def is_subgroup_label(label: str, group_code: str) -> bool:
        """
        Запись в списке «студентов» — это подгруппа, а не человек?

        Подгруппами сайт заводит потоки иностранного языка: группа учит
        разные языки, у каждого потока свой преподаватель и своё время,
        и одним занятием на всю группу это не показать. Метка подгруппы —
        код группы с номером: «мг51соврс-1». Фамилия так выглядеть не может.
        """
        if not label or not group_code:
            return False
        pattern = re.escape(group_code.strip().lower()) + r'\s*-\s*\d+$'
        return re.match(pattern, label.strip().lower()) is not None

    def _find_roster(self, html: str, group_code: str = '') -> dict:
        """
        Разобрать список, который сайт отдаёт для группы в режиме студента.

        В нём лежат две разные сущности с одинаковым обращением ?selst=N:
        живые студенты и подгруппы — потоки иностранного языка. Отличаются
        только меткой: у подгруппы это код группы с номером, «с101-3».
        У подгруппы при этом непустой title, так что без явной проверки
        она попадает в список студентов и всплывает в боте среди фамилий.

        Возвращает {'people': [...], 'subgroups': [...]}.
        """
        soup = BeautifulSoup(html, 'html.parser')
        people, subgroups = [], []

        for tr in soup.find_all('tr', onclick=self.SELST_LINK_RE):
            m = self.SELST_LINK_RE.search(tr.get('onclick', ''))
            if not m:
                continue

            tds = tr.find_all('td')
            if len(tds) < 2:
                continue

            name_td = tds[1]
            full_name = (name_td.get('title') or '').strip()
            short_name = name_td.get_text(strip=True)
            label = short_name or full_name

            if self.is_subgroup_label(label, group_code):
                subgroups.append({'site_id': m.group(1), 'label': label})
            elif full_name:
                people.append({
                    'site_id': m.group(1),
                    'full_name': full_name,
                    'short_name': short_name,
                })

        return {'people': people, 'subgroups': subgroups}

    def _find_students(self, html: str, group_code: str = '') -> list:
        """Только живые студенты: подгруппы отсеиваются."""
        return self._find_roster(html, group_code)['people']

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

    def _repair_lesson_titles(self, html: str) -> str:
        """Экранировать кавычки внутри title занятия, прежде чем парсить."""
        def fix(m):
            head, inner, tail = m.group(1), m.group(2), m.group(3)
            if '"' not in inner:
                return m.group(0)
            self.repaired_titles += 1
            return head + inner.replace('"', '&quot;') + tail

        return self.LESSON_TITLE_RE.sub(fix, html)

    def _pair_numbers(self, day_table, cell_count: int):
        """
        Настоящие номера пар для таблицы дня, или None, если не вышло.

        Позицию ячейки использовать нельзя: сайт выбрасывает пустые верхние
        строки. У мг54САГУсд день начинается со второй пары, строки первой
        нет вовсе — и `pair = i + 1` сдвигал весь день на пару вверх, ставя
        человеку девятичасовую пару вместо десяти сорока. Проверено
        12.09.2026 на скриншоте с сайта: там 2,3,4,5,6, в базе лежало 1,2,3,5.

        Номера есть в разметке — в колонке-линейке, общей на всю строку дня
        недели (SITE.md §7). Ищем их у ближайшего предка-строки: каждый
        номер подписан временем в title, и по этому признаку он отличается
        от цифры, случайно оказавшейся в ячейке занятия.

        Длина линейки обязана совпасть с числом ячеек дня — иначе
        соответствие «строка к строке» не гарантировано, и лучше честно
        отказаться, чем молча разложить занятия не по тем парам.
        """
        week_row = day_table.find_parent('tr')
        while week_row is not None:
            numbers = [
                int(td.get_text(strip=True))
                for td in week_row.find_all('td')
                if self.PAIR_NUM_RE.match(td.get_text(strip=True) or '')
                and self.PAIR_TIME_TITLE_RE.match((td.get('title') or '').strip())
            ]
            if len(numbers) == cell_count:
                return numbers
            if numbers:
                # линейка нашлась, но не той длины — это уже не наш случай
                return None
            week_row = week_row.find_parent('tr')
        return None

    def _parse_page(self, html):
        soup = BeautifulSoup(self._repair_lesson_titles(html), 'html.parser')
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

            cells = table.find_all('td', class_=self.PAIR_CELL_CLASS)
            numbers = self._pair_numbers(table, len(cells))
            if numbers is None:
                self._note_missing_ruler(raw_date, len(cells))
                numbers = list(range(1, len(cells) + 1))

            for i, cell in enumerate(cells):
                pair = numbers[i]
                if is_wednesday and pair in PAIR_TIMES_WED_MFK:
                    t_start, t_end = PAIR_TIMES_WED_MFK[pair]
                else:
                    t_start, t_end = PAIR_TIMES.get(pair, ('?', '?'))

                for div in cell.find_all('div', id=self.LESSON_ID):
                    parsed = self._parse_lesson(div, iso_date, pair, t_start, t_end)
                    if parsed:
                        lessons.append(parsed)
                    else:
                        # Раньше такой блок исчезал молча — главный канал
                        # незаметной потери данных на этом сайте
                        self._note_unparsed(div.get('title', ''), iso_date, pair)

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
