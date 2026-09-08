#!/usr/bin/env python3
"""
Тесты устойчивости парсера. Сети не требуют: поднимают мини-копию сайта
на localhost (tests/fake_site.py).

Запуск:  python -m unittest discover -s tests -v
   или:  python tests/test_parser_resilience.py
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import parsers.base as base
from parsers.base import FetchError
from parsers.socio import SocioParser
from core.database import _create_tables, save_lessons, count_lessons

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
