#!/usr/bin/env python3
"""
Тесты устойчивости парсера. Сети не требуют: поднимают мини-копию сайта
на localhost (tests/fake_site.py).

Запуск:  python -m unittest discover -s tests -v
   или:  python tests/test_parser_resilience.py
"""

import os
import signal
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parsers.base as base
from parsers.base import FetchError
from parsers.socio import SocioParser
from core.database import (_create_tables, save_lessons, count_lessons, _migrate,
                           get_or_create_group,
                           save_subgroup_lessons)
from core.db_students import update_lesson_teachers
import run_parser

from tests import fake_site
from tests.fake_site import FakeSite, day_table, lesson_div, broken_div


def month_qs(offset=0):
    today = date.today()
    m, y = today.month + offset, today.year
    if m > 12:
        m -= 12
        y += 1
    return f"{m}.{y}"


class ParserTestCase(unittest.TestCase):
    """Общая обвязка: быстрый парсер без вежливых пауз, свой сайт на localhost."""

    def make_parser(self, domain):
        parser = SocioParser()
        parser.DOMAIN = domain
        parser.session.trust_env = False   # мимо системного прокси
        self.addCleanup(parser.session.close)
        return parser

    def setUp(self):
        # Иначе тесты ждут по 2-3 секунды на каждый ретрай
        self._saved = (base.PARSER_REQUEST_DELAY, base.PARSER_RETRY_BACKOFF)
        base.PARSER_REQUEST_DELAY = 0
        base.PARSER_RETRY_BACKOFF = 0.01

    def tearDown(self):
        base.PARSER_REQUEST_DELAY, base.PARSER_RETRY_BACKOFF = self._saved


class TestDownloadRetries(ParserTestCase):
    """Ретраи в download() — пункт «один таймаут = тихая дыра»."""

    def setUp(self):
        super().setUp()
        self.site = FakeSite()
        self.server, self.domain = fake_site.start(
            self.site, lambda qs, key: '<p>ok</p>')
        self.addCleanup(fake_site.stop, self.server)

    def test_recovers_after_temporary_failures(self):
        self.site.fail_times['index'] = 2      # два раза 503, потом ок
        parser = self.make_parser(self.domain)

        html = parser.download('/index.php')

        self.assertIn('ok', html)
        self.assertEqual(parser.requests_ok, 1)
        self.assertEqual(parser.requests_failed, 0)
        self.assertEqual(parser.retries_used, 2)
        self.assertEqual(self.site.hits['index'], 3)

    def test_gives_up_and_counts_failure(self):
        self.site.fail_times['index'] = 99     # сайт лежит совсем
        parser = self.make_parser(self.domain)

        html = parser.download('/index.php')

        self.assertEqual(html, '')
        self.assertEqual(parser.requests_failed, 1)
        self.assertEqual(self.site.hits['index'], base.PARSER_MAX_ATTEMPTS)
        self.assertTrue(parser.failed_urls, "неудачный URL должен попасть в отчёт")

    def test_required_page_raises_instead_of_silent_empty(self):
        """Критичная страница не должна тихо возвращать '' — иначе запишем пустоту и скажем 'ok'."""
        self.site.fail_times['index'] = 99
        parser = self.make_parser(self.domain)

        with self.assertRaises(FetchError):
            parser.download('/index.php', required=True)

    def test_no_retry_on_404(self):
        """404 повторять бессмысленно — не тратим на него четыре попытки."""
        self.site.status_for['index'] = 404
        parser = self.make_parser(self.domain)

        html = parser.download('/index.php')

        self.assertEqual(html, '')
        self.assertEqual(self.site.hits['index'], 1)
        self.assertEqual(parser.retries_used, 0)


class TestUnparsedBlocks(ParserTestCase):
    """Счётчик нераспознанных блоков — раньше такие занятия исчезали молча."""

    def test_counts_and_samples_broken_blocks(self):
        parser = SocioParser()
        html = day_table('08.09.2026', [
            [lesson_div('Социология', 'СОЦ', '301', 'Лк', 'с101', 'Иванов И.И.')],
            [broken_div('что-то не по формату')],
        ])

        lessons = parser._parse_page(html)

        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]['subject'], 'Социология')
        self.assertEqual(lessons[0]['teacher'], 'Иванов И.И.')
        self.assertEqual(lessons[0]['room'], '301')
        self.assertEqual(parser.unparsed_blocks, 1)
        self.assertEqual(len(parser.unparsed_samples), 1)
        self.assertIn('не по формату', parser.unparsed_samples[0])


class TestBrokenTitleRepair(ParserTestCase):
    """
    Разметка, снятая с живого сайта 08.09.2026. Первый случай стоил
    106 потерянных занятий за прогон — их находил счётчик нераспознанных
    блоков, но сами занятия молча пропадали.
    """

    @staticmethod
    def wrap(div):
        return ('<table><tr><td>08.09.2026</td></tr>'
                f'<tr><td class="TmTblC">{div}</td></tr></table>')

    BODY = ('<font color="#004000"><b>АСИ</b></font>'
            '<b>301</b><font>Лк</font>[с301]Иванов И.И.')

    def test_unescaped_quotes_in_title(self):
        """title="Лекция по 'Анализ ... в программе "Статистический пакет..."'" """
        div = ('<div id="LESS" title="Лекция по \'Анализ статистической информации '
               'в программе "Статистический пакет для социальных наук"\'">'
               + self.BODY + '</div>')

        parser = SocioParser()
        lessons = parser._parse_page(self.wrap(div))

        self.assertEqual(len(lessons), 1, "занятие больше не теряется")
        self.assertEqual(
            lessons[0]['subject'],
            'Анализ статистической информации в программе '
            '"Статистический пакет для социальных наук"')
        self.assertEqual(lessons[0]['teacher'], 'Иванов И.И.')
        self.assertEqual(parser.unparsed_blocks, 0)
        self.assertEqual(parser.repaired_titles, 1)

    def test_style_glued_to_id_and_added_suffix(self):
        """Второй реальный случай: style без пробела после id и хвост «Добавлено»."""
        div = ('<div id="LESS"style="border:1px solid red" '
               'title="Лекция по \'Социальное пространство современных городов\' '
               'Добавлено 03.09.2026,14:20">' + self.BODY + '</div>')

        parser = SocioParser()
        lessons = parser._parse_page(self.wrap(div))

        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]['subject'],
                         'Социальное пространство современных городов')
        self.assertEqual(parser.unparsed_blocks, 0)

    def test_normal_title_untouched(self):
        div = ('<div id="LESS" title="Семинар по \'Методология\'">'
               + self.BODY + '</div>')
        parser = SocioParser()
        lessons = parser._parse_page(self.wrap(div))

        self.assertEqual(lessons[0]['subject'], 'Методология')
        self.assertEqual(parser.repaired_titles, 0, "чинить было нечего")

    def test_repair_does_not_touch_other_tags(self):
        """У td и a title не последний атрибут — их той же меркой чинить нельзя."""
        html = '<td title="Иванов Иван Иванович" class="name">Иванов И.И.</td>'
        parser = SocioParser()
        self.assertEqual(parser._repair_lesson_titles(html), html)
        self.assertEqual(parser.repaired_titles, 0)

    def test_repair_is_per_lesson_not_across_page(self):
        """Починка одного блока не должна съедать соседний."""
        broken = ('<div id="LESS" title="Лекция по \'А "Б" В\'">' + self.BODY + '</div>')
        ok = ('<div id="LESS" title="Семинар по \'Обычный предмет\'">' + self.BODY + '</div>')

        parser = SocioParser()
        lessons = parser._parse_page(self.wrap(broken + ok))

        self.assertEqual([l['subject'] for l in lessons],
                         ['А "Б" В', 'Обычный предмет'])
        self.assertEqual(parser.repaired_titles, 1)
        self.assertEqual(parser.unparsed_blocks, 0)


class TestSubgroupDetection(unittest.TestCase):
    """
    В списке «студентов» сайт держит и живых людей, и подгруппы —
    потоки иностранного языка. Обращение к ним одинаковое (?selst=N),
    отличить можно только по метке.
    """

    def test_subgroup_labels(self):
        for label, code in [('мг51соврс-1', 'мг51соврс'),
                            ('мг55кпсм-2', 'мг55кпсм'),
                            ('МГ56САСИД-3', 'мг56сасид'),      # регистр не важен
                            ('мг51соврс - 1', 'мг51соврс'),    # пробелы вокруг дефиса
                            ('с101-1', 'с101')]:
            self.assertTrue(SocioParser.is_subgroup_label(label, code), label)

    def test_people_are_not_subgroups(self):
        for label in ('Авдохина С.Б.', 'Смирнов-Петров А.Б.',
                      'Иванова Мария Сергеевна', 'мг51соврс'):
            self.assertFalse(SocioParser.is_subgroup_label(label, 'мг51соврс'), label)

    def test_empty_input(self):
        self.assertFalse(SocioParser.is_subgroup_label('', 'мг51соврс'))
        self.assertFalse(SocioParser.is_subgroup_label('мг51соврс-1', ''))

    def test_other_group_code_does_not_match(self):
        """Подгруппа чужой группы — не подгруппа этой."""
        self.assertFalse(SocioParser.is_subgroup_label('мг55кпсм-1', 'мг51соврс'))


class TestShrinkGuard(unittest.TestCase):
    """Защита от затирания: огрызок с сайта не должен стирать нормальные данные."""

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        _create_tables(self.conn)
        self.addCleanup(self.conn.close)

    @staticmethod
    def lessons(n, dt='2026-09-08'):
        return [{
            'date': dt, 'pair_number': i + 1, 'time_start': '09:00', 'time_end': '10:30',
            'subject': f'Предмет {i}', 'subject_abbr': 'П', 'lesson_type': 'Лк',
            'lesson_type_full': 'Лекция', 'room': '101', 'teacher': 'Иванов И.И.',
        } for i in range(n)]

    def test_full_write_goes_through(self):
        res = save_lessons(self.conn, 1, self.lessons(10))
        self.assertEqual(res['written'], 10)
        self.assertFalse(res['skipped'])
        self.assertEqual(count_lessons(self.conn, 1), 10)

    def test_suspicious_shrink_is_refused(self):
        save_lessons(self.conn, 1, self.lessons(10))
        res = save_lessons(self.conn, 1, self.lessons(2))

        self.assertTrue(res['skipped'])
        self.assertIn('против 10', res['reason'])
        self.assertEqual(count_lessons(self.conn, 1), 10, "старые данные должны уцелеть")

    def test_moderate_change_is_allowed(self):
        save_lessons(self.conn, 1, self.lessons(10))
        res = save_lessons(self.conn, 1, self.lessons(8))

        self.assertFalse(res['skipped'])
        self.assertEqual(count_lessons(self.conn, 1), 8)

    def test_guard_can_be_switched_off(self):
        save_lessons(self.conn, 1, self.lessons(10))
        res = save_lessons(self.conn, 1, self.lessons(1), shrink_guard=False)

        self.assertFalse(res['skipped'])
        self.assertEqual(count_lessons(self.conn, 1), 1)


class TestIncrementalSave(ParserTestCase):
    """
    Главное: прогон больше не «всё или ничего».
    Проверяем, что данные видны в базе ИЗ ДРУГОГО СОЕДИНЕНИЯ до конца прогона —
    то есть переживут падение процесса.
    """

    GROUPS = {'101': 'с101', '102': 'с102', '103': 'с103'}

    def setUp(self):
        super().setUp()
        self.site = FakeSite()
        self.server, self.domain = fake_site.start(self.site, self.body_for)
        self.addCleanup(fake_site.stop, self.server)

        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(self.db_path) and os.remove(self.db_path))

        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        _create_tables(self.conn)
        self.addCleanup(self.conn.close)

    def body_for(self, qs, key):
        if 'pMns' in qs:
            if qs['pMns'][0] != month_qs(0):
                return '<p>нет данных</p>'    # второй месяц пустой
            return day_table('08.09.2026', [
                [lesson_div('Социология', 'СОЦ', '301', 'Лк', 'с101', 'Иванов И.И.')],
                [lesson_div('Статистика', 'СТАТ', '302', 'Сем', 'с101', 'Петров П.П.')],
            ])
        if 'gr' in qs:
            return '<p>страница группы</p>'
        if 'yr' in qs:
            return ''.join(f'<a href="?gr={gid}">{code}</a>'
                           for gid, code in self.GROUPS.items())
        if 'f' in qs:
            return '<select name="yr"><option value="2026">2026</option></select>'
        return '<a href="?f=1">Бакалавриат</a>'

    def _on_group_saver(self, seen_from_outside):
        """Колбэк как в run_socio: пишет группу в базу сразу."""
        def on_group(g):
            gid = self.conn.execute(
                "INSERT INTO groups_ (faculty_id, code, site_id) VALUES (1, ?, ?)",
                (g['code'], g['site_id'])
            ).lastrowid
            self.conn.commit()
            save_lessons(self.conn, gid, g['lessons'])

            # Смотрим базу отдельным соединением: то, что видно отсюда,
            # переживёт смерть процесса
            probe = sqlite3.connect(self.db_path)
            probe.row_factory = sqlite3.Row
            seen_from_outside.append(
                probe.execute("SELECT COUNT(*) AS c FROM lessons").fetchone()['c'])
            probe.close()
        return on_group

    def test_each_group_lands_in_db_immediately(self):
        parser = self.make_parser(self.domain)
        self.conn.execute(
            "INSERT INTO faculties (id, code, name, domain) VALUES (1, 'socio', 'Соцфак', ?)",
            (self.domain,))
        self.conn.commit()

        seen = []
        parser.parse(on_group=self._on_group_saver(seen))

        # 3 группы × 2 занятия, и после каждой группы данные уже на диске
        self.assertEqual(seen, [2, 4, 6])
        self.assertEqual(count_lessons(self.conn, 1), 2)

    def test_one_broken_group_does_not_kill_the_run(self):
        parser = self.make_parser(self.domain)
        self.conn.execute(
            "INSERT INTO faculties (id, code, name, domain) VALUES (1, 'socio', 'Соцфак', ?)",
            (self.domain,))
        self.conn.commit()

        original = parser._fetch_group_schedule

        def explode_on_second(group_url):
            if 'gr=102' in group_url:
                raise RuntimeError('сайт икнул на этой группе')
            return original(group_url)

        parser._fetch_group_schedule = explode_on_second

        seen = []
        result = parser.parse(on_group=self._on_group_saver(seen))

        codes = [g['code'] for g in result['groups']]
        self.assertEqual(codes, ['с101', 'с103'], "упавшая группа пропущена, остальные собраны")
        self.assertEqual(seen, [2, 4])
        self.assertTrue(parser.failed_urls, "падение группы должно попасть в отчёт")

    def test_dead_site_raises_instead_of_reporting_empty_success(self):
        """Главная не отвечает → FetchError, а не тихий 'собрано 0 групп, всё ок'."""
        self.site.fail_times['index'] = 99
        parser = self.make_parser(self.domain)

        with self.assertRaises(FetchError):
            parser.parse(on_group=lambda g: None)


if __name__ == '__main__':
    unittest.main(verbosity=2)


class TestTeacherSurvivesRewrite(unittest.TestCase):
    """
    Прогон socio не должен стирать преподавателей.

    Страницы групп преподавателя не содержат вовсе (docs/SITE.md §5),
    поэтому socio приносит пустое поле, а имена ставит отдельный проход
    teachers. Пока save_lessons не переносил их через DELETE+INSERT, каждый
    прогон socio обнулял результат прохода по кафедрам: в базе оставалось
    ноль имён до следующей ночи.
    """

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        _create_tables(self.conn)
        self.addCleanup(self.conn.close)

    @staticmethod
    def lessons(n, teacher='', dt='2026-09-08'):
        return [{
            'date': dt, 'pair_number': i + 1, 'time_start': '09:00', 'time_end': '10:30',
            'subject': f'Предмет {i}', 'subject_abbr': 'П', 'lesson_type': 'Лк',
            'lesson_type_full': 'Лекция', 'room': '101', 'teacher': teacher,
        } for i in range(n)]

    def teachers(self):
        return [r['teacher'] for r in self.conn.execute(
            "SELECT teacher FROM lessons WHERE group_id = 1 ORDER BY pair_number")]

    def test_socio_rerun_keeps_teachers(self):
        """Главное: имена, проставленные проходом teachers, переживают socio."""
        save_lessons(self.conn, 1, self.lessons(6))
        update_lesson_teachers(self.conn, [{
            'teacher': 'Сушко В.А.', 'group_id': 1, 'date': '2026-09-08',
            'pair_number': 3, 'subject': 'Предмет 2',
        }])
        self.assertEqual(self.teachers().count('Сушко В.А.'), 1)

        # тот же socio ещё раз: занятия те же, преподавателя он не знает
        res = save_lessons(self.conn, 1, self.lessons(6))

        self.assertEqual(res['teachers_kept'], 1)
        self.assertEqual(self.teachers().count('Сушко В.А.'), 1,
                         "socio стёр преподавателя, поставленного проходом teachers")

    def test_own_teacher_wins_over_kept(self):
        """Имя из свежих данных сильнее перенесённого: источник знает лучше."""
        save_lessons(self.conn, 1, self.lessons(2, teacher='Старый С.С.'))
        res = save_lessons(self.conn, 1, self.lessons(2, teacher='Новый Н.Н.'))

        self.assertEqual(res['teachers_kept'], 0)
        self.assertEqual(set(self.teachers()), {'Новый Н.Н.'})

    def test_changed_lesson_does_not_inherit_teacher(self):
        """Другой предмет в той же паре — другое занятие, имя не наследуется."""
        save_lessons(self.conn, 1, self.lessons(2, teacher='Сушко В.А.'))
        replaced = self.lessons(2)
        replaced[0]['subject'] = 'Совсем другой предмет'
        res = save_lessons(self.conn, 1, replaced)

        self.assertEqual(res['teachers_kept'], 1, "уцелеть должно только второе занятие")
        by_subject = {r['subject']: r['teacher'] for r in self.conn.execute(
            "SELECT subject, teacher FROM lessons WHERE group_id = 1")}
        self.assertEqual(by_subject['Совсем другой предмет'], '')
        self.assertEqual(by_subject['Предмет 1'], 'Сушко В.А.')

    def test_guarded_run_reports_zero_kept(self):
        """Огрызок ничего не переписывает — и переносить ему нечего."""
        save_lessons(self.conn, 1, self.lessons(10, teacher='Сушко В.А.'))
        res = save_lessons(self.conn, 1, self.lessons(2))

        self.assertTrue(res['skipped'])
        self.assertEqual(res['teachers_kept'], 0)
        self.assertEqual(self.teachers().count('Сушко В.А.'), 10)

    def test_subgroup_lessons_are_untouched(self):
        """Занятия подгрупп socio не трогает — их имена не при чём."""
        save_lessons(self.conn, 1, self.lessons(2))
        save_subgroup_lessons(self.conn, 1, 'с101-1', [{
            'date': '2026-09-08', 'pair_number': 5, 'time_start': '15:00',
            'time_end': '16:30', 'subject': 'Английский язык', 'subject_abbr': 'Англ',
            'lesson_type': 'Пр', 'lesson_type_full': 'Практика', 'room': '202',
            'teacher': 'Авдохина С.Б.',
        }])
        save_lessons(self.conn, 1, self.lessons(2))

        kept = self.conn.execute(
            "SELECT teacher FROM lessons WHERE subgroup = 'с101-1'").fetchone()
        self.assertEqual(kept['teacher'], 'Авдохина С.Б.')


class TestSignalBecomesException(unittest.TestCase):
    """
    Прогон, убитый сигналом, обязан отметиться в parse_log.

    09.09.2026 systemd убил проход по студентам по TimeoutStartSec: 639
    студентов из 1101 уже лежали в базе, а снаружи не было ни записи
    в журнале, ни алерта. Ветки перехвата в run_* ловят Exception, но SIGTERM
    исключения не поднимает, а KeyboardInterrupt от Ctrl+C наследуется
    от BaseException и проходит мимо. Оба случая закрываются одинаково —
    обработчиком, который поднимает Interrupted.
    """

    def setUp(self):
        # обработчики глобальные: вернуть как было, иначе поломаем соседей
        self.saved = {s: signal.getsignal(s)
                      for s in (signal.SIGTERM, signal.SIGINT)}
        self.addCleanup(lambda: [signal.signal(s, h)
                                 for s, h in self.saved.items()])

    def test_interrupted_is_catchable_as_exception(self):
        """Главное: иначе ветка partial так и не отработает."""
        self.assertTrue(issubclass(run_parser.Interrupted, Exception))

    def test_sigterm_handler_raises(self):
        run_parser._install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        self.assertTrue(callable(handler), "обработчик SIGTERM не поставлен")

        with self.assertRaises(run_parser.Interrupted) as ctx:
            handler(signal.SIGTERM, None)
        self.assertIn('SIGTERM', str(ctx.exception))

    def test_sigint_handler_raises(self):
        """Ctrl+C молчал по той же причине, что и systemd."""
        run_parser._install_signal_handlers()
        handler = signal.getsignal(signal.SIGINT)

        with self.assertRaises(run_parser.Interrupted) as ctx:
            handler(signal.SIGINT, None)
        self.assertIn('SIGINT', str(ctx.exception))

    def test_handler_result_reaches_partial_branch(self):
        """Проверка формы: то, что поднимает обработчик, ловится как Exception."""
        run_parser._install_signal_handlers()
        handler = signal.getsignal(signal.SIGTERM)

        caught = None
        try:
            handler(signal.SIGTERM, None)
        except Exception as e:          # ровно так написаны ветки в run_*
            caught = e
        self.assertIsInstance(caught, run_parser.Interrupted)


class TestGroupLastSeen(unittest.TestCase):
    """
    Отметка «группа ещё есть на сайте».

    Наборы выпускаются, коды исчезают из навигации, а строка в базе остаётся
    навсегда. Без отметки бот не отличал «расписание ещё не выложили»
    от «такой группы больше нет» и бодро писал «🎉 Нет занятий!» подписчику
    пп402 каждый день (docs/TODO.md §1).
    """

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        # база, собранная до появления колонки
        self.conn.executescript("""
            CREATE TABLE faculties (
                id INTEGER PRIMARY KEY, code TEXT, name TEXT, domain TEXT);
            CREATE TABLE groups_ (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                faculty_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                site_id TEXT, department TEXT, program TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(faculty_id, code));
        """)
        self.conn.execute(
            "INSERT INTO faculties (id,code,name,domain) VALUES (1,'socio','Соцфак','x')")
        for code in ('с403', 'пп402'):
            self.conn.execute(
                "INSERT INTO groups_ (faculty_id, code) VALUES (1, ?)", (code,))
        self.conn.commit()

    def last_seen(self, code):
        return self.conn.execute(
            "SELECT last_seen FROM groups_ WHERE code = ?", (code,)).fetchone()['last_seen']

    def test_migration_marks_existing_groups_alive(self):
        """Иначе до первого обхода сайта пропавшими выглядели бы все сразу."""
        _migrate(self.conn)
        today = date.today().isoformat()
        self.assertEqual(self.last_seen('с403'), today)
        self.assertEqual(self.last_seen('пп402'), today)

    def test_parse_refreshes_only_groups_still_on_site(self):
        """Обход обновляет живые; пропавшую обновить нечем — она отстаёт."""
        _migrate(self.conn)
        stale = (date.today() - timedelta(days=30)).isoformat()
        self.conn.execute("UPDATE groups_ SET last_seen = ?", (stale,))
        self.conn.commit()

        # сайт отдал в навигации только эту группу
        get_or_create_group(self.conn, faculty_id=1, code='с403')

        self.assertEqual(self.last_seen('с403'), date.today().isoformat())
        self.assertEqual(self.last_seen('пп402'), stale,
                         "пропавшую группу обход трогать не должен")

    def test_new_group_is_marked_on_creation(self):
        _migrate(self.conn)
        get_or_create_group(self.conn, faculty_id=1, code='с101')
        self.assertEqual(self.last_seen('с101'), date.today().isoformat())

    def test_migration_runs_once(self):
        """Повторный вызов не должен воскрешать пропавшую группу."""
        _migrate(self.conn)
        stale = (date.today() - timedelta(days=30)).isoformat()
        self.conn.execute("UPDATE groups_ SET last_seen = ?", (stale,))
        self.conn.commit()

        _migrate(self.conn)

        self.assertEqual(self.last_seen('пп402'), stale,
                         "миграция сработала второй раз и затёрла отметку")
