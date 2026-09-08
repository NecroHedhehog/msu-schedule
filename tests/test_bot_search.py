#!/usr/bin/env python3
"""
Тесты поиска в боте: студент, преподаватель, группа.

Сеть и Telegram не нужны. Проверяется то, из-за чего 27% преподавателей
были недостижимы: поиск студента перехватывал поиск преподавателя, потому
что оба жили в одном обработчике с одинаковым условием.

Запуск:  python -m unittest discover -s tests -t .
"""

import os
import sqlite3
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import _create_tables, _register_functions, find_groups_by_code
from core.db_students import (
    ensure_tables, get_students_by_name, find_teachers_by_name,
)
from bot.formatting import empty_day_reason


class SearchTestCase(unittest.TestCase):
    """База с однофамильцами: студент Смирнов и преподаватель Смирнов."""

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        _register_functions(self.conn)
        _create_tables(self.conn)
        ensure_tables(self.conn)
        self.addCleanup(self.conn.close)

        self.conn.execute(
            "INSERT INTO faculties (id, code, name, domain) VALUES (1,'socio','Соцфак','http://x')")
        for gid, code in [(1, 'с403'), (2, 'с101'), (3, 'пп403')]:
            self.conn.execute(
                "INSERT INTO groups_ (id, faculty_id, code, department, program) "
                "VALUES (?, 1, ?, 'Бакалавриат', 'Соц')", (gid, code))

        for site_id, name in [
            ('101', 'Смирнов Алексей Петрович'),
            ('102', 'Смирнова Дарья Ивановна'),
            ('103', 'Иванова Мария Сергеевна'),
        ]:
            self.conn.execute(
                "INSERT INTO students (group_id, site_id, full_name, short_name) "
                "VALUES (1, ?, ?, ?)", (site_id, name, name.split()[0]))

        for pair, subject, teacher in [
            (1, 'Социология', 'Смирнов В.А.'),
            (2, 'Статистика', 'Осипова Н.Г., Елишев С.О.'),
            (3, 'Физическая культура', ''),
        ]:
            self.conn.execute(
                """INSERT INTO lessons (group_id, date, pair_number, time_start, time_end,
                                        subject, teacher)
                   VALUES (1, '2026-09-08', ?, '09:00', '10:30', ?, ?)""",
                (pair, subject, teacher))
        self.conn.commit()


class TestTeacherNotShadowedByStudent(SearchTestCase):
    """Главный баг: однофамилец-студент заслонял преподавателя."""

    def test_teacher_found_despite_namesake_students(self):
        teachers = find_teachers_by_name(self.conn, 'Смирнов')
        self.assertEqual(teachers, ['Смирнов В.А.'])

    def test_students_found_independently(self):
        students = get_students_by_name(self.conn, 'Смирнов')
        names = sorted(s['full_name'] for s in students)
        self.assertEqual(names, ['Смирнов Алексей Петрович', 'Смирнова Дарья Ивановна'])

    def test_two_searches_do_not_interfere(self):
        """Оба поиска по одной фамилии возвращают своё и ничего не съедают."""
        self.assertTrue(find_teachers_by_name(self.conn, 'Смирнов'))
        self.assertTrue(get_students_by_name(self.conn, 'Смирнов'))


class TestCaseInsensitiveCyrillic(SearchTestCase):
    """
    Встроенный LOWER() в SQLite кириллицу не трогает, поэтому LIKE был
    регистрозависимым и фамилия с маленькой буквы не находила никого.
    """

    def test_teacher_any_case(self):
        for q in ('Смирнов', 'смирнов', 'СМИРНОВ', 'сМиРнОв'):
            self.assertEqual(find_teachers_by_name(self.conn, q), ['Смирнов В.А.'], q)

    def test_student_any_case(self):
        for q in ('Иванова', 'иванова', 'ИВАНОВА'):
            found = get_students_by_name(self.conn, q)
            self.assertEqual([s['full_name'] for s in found],
                             ['Иванова Мария Сергеевна'], q)

    def test_sqlite_lower_really_is_broken(self):
        """Фиксируем причину, чтобы pylower не убрали как лишний."""
        self.assertEqual(self.conn.execute("SELECT lower('ИВАНОВА')").fetchone()[0], 'ИВАНОВА')
        self.assertEqual(self.conn.execute("SELECT pylower('ИВАНОВА')").fetchone()[0], 'иванова')


class TestTeacherNameSplitting(SearchTestCase):
    """В lessons.teacher бывает несколько человек через запятую."""

    def test_splits_and_returns_only_matching(self):
        self.assertEqual(find_teachers_by_name(self.conn, 'Осипова'), ['Осипова Н.Г.'])
        self.assertEqual(find_teachers_by_name(self.conn, 'Елишев'), ['Елишев С.О.'])

    def test_empty_teacher_ignored(self):
        self.assertEqual(find_teachers_by_name(self.conn, ''), [])
        self.assertEqual(find_teachers_by_name(self.conn, 'Несуществующий'), [])


class TestGroupSearch(SearchTestCase):
    def test_bare_number_gets_prefix(self):
        codes = [r['code'] for r in find_groups_by_code(self.conn, '403')]
        self.assertIn('с403', codes)

    def test_case_insensitive(self):
        self.assertTrue(find_groups_by_code(self.conn, 'С403'))
        self.assertTrue(find_groups_by_code(self.conn, 'с403'))

    def test_unknown_code(self):
        self.assertEqual(find_groups_by_code(self.conn, 'зз999'), [])


class TestEmptyDayReason(unittest.TestCase):
    """«Нет занятий» больше не значит одновременно выходной и дыру в данных."""

    RANGE = ('2026-09-01', '2026-10-30')

    def test_beyond_collected_data(self):
        self.assertIn('ещё не выложено', empty_day_reason(date(2026, 11, 15), self.RANGE))

    def test_before_collected_data(self):
        self.assertIn('Данных за этот день нет', empty_day_reason(date(2026, 3, 10), self.RANGE))

    def test_sunday_inside_range(self):
        self.assertIn('Воскресенье', empty_day_reason(date(2026, 9, 13), self.RANGE))

    def test_weekday_inside_range(self):
        self.assertIn('Нет занятий', empty_day_reason(date(2026, 9, 10), self.RANGE))

    def test_without_range_falls_back(self):
        self.assertIn('Нет занятий', empty_day_reason(date(2026, 9, 10), None))


class TestHandlerWiring(unittest.TestCase):
    """
    Каждый поиск должен висеть на своём состоянии, а общий обработчик —
    идти последним и без состояния.
    """

    @classmethod
    def setUpClass(cls):
        import bot.main as bot_main
        cls.m = bot_main
        cls.handlers = bot_main.router.message.handlers

    @staticmethod
    def states_of(handler):
        from aiogram.fsm.state import State
        return [f.callback for f in (handler.filters or [])
                if isinstance(f.callback, State)]

    def by_name(self, name):
        for h in self.handlers:
            if h.callback.__name__ == name:
                return h
        self.fail(f"обработчик {name} не зарегистрирован")

    def test_each_query_handler_has_its_state(self):
        expected = {
            'on_teacher_query': self.m.Search.teacher,
            'on_student_query': self.m.Search.student,
            'on_group_query': self.m.Search.group,
        }
        for name, state in expected.items():
            self.assertEqual(self.states_of(self.by_name(name)), [state], name)

    def test_catch_all_has_no_state_and_goes_last(self):
        self.assertEqual(self.states_of(self.handlers[-1]), [])
        self.assertEqual(self.handlers[-1].callback.__name__, 'on_text_message')

    def test_buttons_are_registered_before_state_handlers(self):
        """Кнопка нижней клавиатуры должна перебивать ожидание текста."""
        order = [h.callback.__name__ for h in self.handlers]
        for button_handler in ('cmd_today', 'cmd_week', 'cmd_change_group'):
            self.assertLess(order.index(button_handler), order.index('on_teacher_query'),
                            button_handler)


if __name__ == '__main__':
    unittest.main(verbosity=2)
