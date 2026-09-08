#!/usr/bin/env python3
"""
Подгруппы — языковые потоки.

Сайт заводит их отдельными сущностями и кладёт в тот же список, что и живых
студентов, с тем же обращением ?selst=N. Разведка 08.09.2026 показала:
13 групп из 49 имеют подгруппы, и 90 из них попали бы в таблицу students —
у подгруппы непустой title, так что без явной проверки человек увидел бы
«с101-3» среди фамилий.

Занятия потоков живут в той же таблице lessons, отличаются колонкой subgroup,
и не должны ни попадать в общий список пар, ни считаться предметами по выбору.
"""

import os
import sqlite3
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import (
    _create_tables, _register_functions, _migrate,
    save_lessons, save_subgroup_lessons, count_lessons,
    delete_streams_by_subject, get_conflicting_subjects, get_lessons_for_date,
)
from bot.formatting import split_streams, format_streams, format_day_schedule
from bot.main import filter_lessons
from parsers.socio import SocioParser


def lesson(pair, subject, dt='2026-09-08', teacher='Иванов И.И.', abbr='ПР'):
    return {'date': dt, 'pair_number': pair, 'time_start': '09:00', 'time_end': '10:30',
            'subject': subject, 'subject_abbr': abbr, 'lesson_type': 'Сем',
            'lesson_type_full': 'Семинар', 'room': '301', 'teacher': teacher}


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        _register_functions(self.conn)
        _create_tables(self.conn)
        self.addCleanup(self.conn.close)


class TestMigration(unittest.TestCase):
    """Колонку subgroup надо доставить в уже существующие базы."""

    def test_adds_missing_columns(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE lessons (id INTEGER PRIMARY KEY, subject TEXT)")
        conn.execute("CREATE TABLE users (chat_id INTEGER PRIMARY KEY)")

        _migrate(conn)

        cols = {r['name'] for r in conn.execute("PRAGMA table_info(lessons)")}
        self.assertIn('subgroup', cols)
        users = {r['name'] for r in conn.execute("PRAGMA table_info(users)")}
        self.assertIn('student_id', users)
        conn.close()

    def test_is_idempotent(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        _create_tables(conn)
        _migrate(conn)
        _migrate(conn)          # второй раз не должен падать
        conn.close()


class TestSubgroupStorage(DbTestCase):
    """Занятия группы и её потоков живут рядом и не затирают друг друга."""

    def test_only_new_lessons_are_kept(self):
        """
        Страница потока показывает и обычное расписание группы тоже.
        Записывать надо только то, чего у группы нет, иначе человек увидит
        каждую пару дважды.
        """
        save_lessons(self.conn, 1, [lesson(1, 'Социология'), lesson(2, 'Статистика')])

        written = save_subgroup_lessons(self.conn, 1, 'с101-3', [
            lesson(1, 'Социология'),                    # дубль группового
            lesson(2, 'Статистика'),                    # дубль группового
            lesson(3, 'Английский язык', teacher='Петрова А.А.'),
        ])

        self.assertEqual(written, 1)
        rows = self.conn.execute(
            "SELECT subject, subgroup FROM lessons WHERE subgroup != ''").fetchall()
        self.assertEqual([(r['subject'], r['subgroup']) for r in rows],
                         [('Английский язык', 'с101-3')])

    def test_group_save_does_not_wipe_streams(self):
        save_lessons(self.conn, 1, [lesson(1, 'Социология')])
        save_subgroup_lessons(self.conn, 1, 'с101-3', [lesson(3, 'Английский язык')])

        # Повторный прогон socio по тем же датам
        save_lessons(self.conn, 1, [lesson(1, 'Социология'), lesson(2, 'Статистика')])

        streams = self.conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE subgroup != ''").fetchone()[0]
        self.assertEqual(streams, 1, "поток должен уцелеть")
        self.assertEqual(count_lessons(self.conn, 1), 3)

    def test_stream_save_does_not_wipe_group(self):
        save_lessons(self.conn, 1, [lesson(1, 'Социология'), lesson(2, 'Статистика')])
        save_subgroup_lessons(self.conn, 1, 'с101-3', [lesson(3, 'Английский язык')])

        own = self.conn.execute(
            "SELECT COUNT(*) FROM lessons WHERE subgroup = ''").fetchone()[0]
        self.assertEqual(own, 2)

    def test_streams_are_independent(self):
        save_lessons(self.conn, 1, [lesson(1, 'Социология')])
        save_subgroup_lessons(self.conn, 1, 'с101-1', [lesson(3, 'Немецкий язык')])
        save_subgroup_lessons(self.conn, 1, 'с101-2', [lesson(3, 'Французский язык')])
        save_subgroup_lessons(self.conn, 1, 'с101-1', [lesson(3, 'Немецкий язык')])  # повтор

        rows = self.conn.execute(
            "SELECT subgroup, subject FROM lessons WHERE subgroup != '' "
            "ORDER BY subgroup").fetchall()
        self.assertEqual([(r['subgroup'], r['subject']) for r in rows],
                         [('с101-1', 'Немецкий язык'), ('с101-2', 'Французский язык')])

    def test_empty_label_refused(self):
        with self.assertRaises(ValueError):
            save_subgroup_lessons(self.conn, 1, '', [lesson(3, 'Английский язык')])


class TestForeignStudentStreams(DbTestCase):
    """
    Потоки для иностранных студентов бот не собирает — не его аудитория.
    Правило по названию предмета, а не по коду группы: коды пересобираются
    каждый семестр, названия предметов живут годами.
    """

    MARKERS = ('Русский язык как иностранный', 'Русский язык')

    def test_detects_foreign_stream(self):
        self.assertEqual(
            SocioParser.foreign_stream_subject([{'subject': 'Русский язык как иностранный'}]),
            'Русский язык как иностранный')

    def test_detects_by_any_lesson(self):
        """У потока мг55кпсм-1 кроме русского была своя программа."""
        lessons = [{'subject': 'Философия'},
                   {'subject': 'Педагогика и психология высшей школы'},
                   {'subject': 'Русский язык'}]
        self.assertEqual(SocioParser.foreign_stream_subject(lessons), 'Русский язык')

    def test_ordinary_stream_passes(self):
        lessons = [{'subject': 'Английский язык'}, {'subject': 'Философия'}]
        self.assertEqual(SocioParser.foreign_stream_subject(lessons), '')

    def test_delete_removes_whole_stream(self):
        """
        Удаляется поток целиком, а не только совпавшие занятия: своя
        программа иностранцев — такой же чужой материал, как и русский.
        """
        save_lessons(self.conn, 1, [lesson(1, 'Социология')])
        save_subgroup_lessons(self.conn, 1, 'мг55кпсм-1', [
            lesson(2, 'Русский язык'),
            lesson(3, 'Философия'),
            lesson(4, 'Педагогика и психология высшей школы'),
        ], only_new=False)
        save_subgroup_lessons(self.conn, 1, 'с101-3',
                              [lesson(5, 'Английский язык')], only_new=False)

        removed = delete_streams_by_subject(self.conn, self.MARKERS)

        self.assertEqual(removed, 3, "весь поток, а не одно занятие")
        left = self.conn.execute(
            "SELECT subgroup, subject FROM lessons WHERE subgroup != ''").fetchall()
        self.assertEqual([(r['subgroup'], r['subject']) for r in left],
                         [('с101-3', 'Английский язык')])

    def test_group_lessons_untouched(self):
        save_lessons(self.conn, 1, [lesson(1, 'Социология'), lesson(2, 'Русский язык')])
        delete_streams_by_subject(self.conn, self.MARKERS)
        self.assertEqual(count_lessons(self.conn, 1), 2,
                         "занятия самой группы чистка не трогает")

    def test_empty_markers_do_nothing(self):
        save_subgroup_lessons(self.conn, 1, 'с101-1',
                              [lesson(2, 'Русский язык')], only_new=False)
        self.assertEqual(delete_streams_by_subject(self.conn, ()), 0)
        self.assertEqual(delete_streams_by_subject(self.conn, ('',)), 0)


class TestStreamsDoNotLookLikeElectives(DbTestCase):
    """
    Потоки всегда стоят на одной паре друг с другом. Если не исключить их,
    механизм «предметов по выбору» примет их за выбор, которого человек
    не делал.
    """

    def test_conflicting_subjects_ignores_streams(self):
        future = (date.today().replace(day=1)).isoformat()
        future = '2099-01-15'
        save_lessons(self.conn, 1, [lesson(1, 'Социология', dt=future)])
        for i, lang in enumerate(('Английский', 'Немецкий', 'Французский'), start=1):
            save_subgroup_lessons(self.conn, 1, f'с101-{i}',
                                  [lesson(3, lang, dt=future)], only_new=False)

        conflicts = get_conflicting_subjects(self.conn, 1)
        self.assertEqual(conflicts, [], "языки — не предметы по выбору")

    def test_subject_filter_keeps_streams(self):
        lessons = [
            {'subject': 'Социология', 'subgroup': ''},
            {'subject': 'Статистика', 'subgroup': ''},
            {'subject': 'Английский язык', 'subgroup': 'с101-3'},
        ]
        kept = filter_lessons(lessons, ['Социология'])
        self.assertEqual([l['subject'] for l in kept],
                         ['Социология', 'Английский язык'])


class TestStreamFormatting(unittest.TestCase):

    STREAM = {'pair_number': 3, 'time_start': '12:20', 'time_end': '13:50',
              'subject': 'Английский язык', 'subject_abbr': 'АНГЛ',
              'lesson_type': 'Сем', 'room': '410', 'teacher': 'Петрова А.А.',
              'subgroup': 'с101-3'}
    MAIN = {'pair_number': 1, 'time_start': '09:00', 'time_end': '10:30',
            'subject': 'Социология', 'subject_abbr': 'СОЦ',
            'lesson_type': 'Лк', 'room': '301', 'teacher': 'Иванов И.И.',
            'subgroup': ''}

    def test_split(self):
        main, streams = split_streams([self.MAIN, self.STREAM])
        self.assertEqual(len(main), 1)
        self.assertEqual(len(streams), 1)

    def test_block_names_the_stream(self):
        text = format_streams([self.STREAM])
        self.assertIn('Не у всех', text)
        self.assertIn('Английский язык', text)
        self.assertIn('Петрова А.А.', text)
        self.assertIn('[3]', text, "номер потока")
        self.assertNotIn('с101-3', text, "код группы человек и так знает")

    def test_grouped_by_subject(self):
        """
        Плоским списком выходило до десяти строк на день: у группы бывает
        семь потоков, и английский идёт сразу у пяти преподавателей.
        """
        streams = [
            dict(self.STREAM, subgroup='с101-2', teacher='Рассошенко Ж.В.', room='320'),
            dict(self.STREAM, subgroup='с101-6', teacher='Казимова Г.А.', room='317'),
            dict(self.STREAM, subject='Немецкий язык', subject_abbr='НЕМ',
                 subgroup='с101-7', teacher='Смирнова М.Д.', room='318'),
        ]
        text = format_streams(streams)

        self.assertEqual(text.count('Английский язык'), 1, "предмет назван один раз")
        self.assertEqual(text.count('Немецкий язык'), 1)
        self.assertIn('[2]', text)
        self.assertIn('[7]', text)

    def test_identical_rows_collapse(self):
        """Два потока в одной аудитории у одного преподавателя — одна строка."""
        same = [dict(self.STREAM, subgroup='с101-1'),
                dict(self.STREAM, subgroup='с101-2')]
        text = format_streams(same)
        self.assertEqual(text.count('Петрова А.А.'), 1)

    def test_day_shows_both_sections(self):
        text = format_day_schedule([self.MAIN, self.STREAM], date(2026, 9, 8),
                                   with_ad=False)
        self.assertIn('СОЦ', text)
        self.assertIn('Не у всех', text)
        self.assertLess(text.index('СОЦ'), text.index('Не у всех'),
                        "потоки идут после обычного расписания")

    def test_day_with_only_streams(self):
        text = format_day_schedule([self.STREAM], date(2026, 9, 8), with_ad=False)
        self.assertIn('Не у всех', text)
        self.assertNotIn('Нет занятий', text)

    def test_empty_day_unchanged(self):
        text = format_day_schedule([], date(2026, 9, 8), with_ad=False)
        self.assertNotIn('Не у всех', text)

    def test_works_with_sqlite_rows(self):
        """В боте занятия приходят как sqlite3.Row, а он не умеет .get()."""
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        _register_functions(conn)
        _create_tables(conn)
        save_lessons(conn, 1, [lesson(1, 'Социология')])
        save_subgroup_lessons(conn, 1, 'с101-3', [lesson(3, 'Английский язык')])

        rows = get_lessons_for_date(conn, 1, '2026-09-08')
        text = format_day_schedule(rows, date(2026, 9, 8), with_ad=False)
        self.assertIn('Не у всех', text)
        self.assertIn('Английский язык', text)
        self.assertIn('[3]', text)
        conn.close()


class TestRosterSplit(unittest.TestCase):
    """Список группы: люди и подгруппы приходят вперемешку."""

    HTML = '''
    <table>
      <tr onclick="location='?selst=101'"><td>1</td>
          <td title="Авдохина Светлана Борисовна">Авдохина С.Б.</td></tr>
      <tr onclick="location='?selst=102'"><td>2</td>
          <td title="с101-1">с101-1</td></tr>
      <tr onclick="location='?selst=103'"><td>3</td>
          <td title="Богданова Ольга Дмитриевна">Богданова О.Д.</td></tr>
      <tr onclick="location='?selst=104'"><td>4</td>
          <td title="с101-2">с101-2</td></tr>
    </table>'''

    def test_people_and_subgroups_separated(self):
        parser = SocioParser()
        roster = parser._find_roster(self.HTML, 'с101')

        self.assertEqual([p['short_name'] for p in roster['people']],
                         ['Авдохина С.Б.', 'Богданова О.Д.'])
        self.assertEqual([s['label'] for s in roster['subgroups']],
                         ['с101-1', 'с101-2'])

    def test_find_students_excludes_subgroups(self):
        """Главное: подгруппа не должна попасть в таблицу students."""
        parser = SocioParser()
        students = parser._find_students(self.HTML, 'с101')

        self.assertEqual(len(students), 2)
        for s in students:
            self.assertFalse(SocioParser.is_subgroup_label(s['short_name'], 'с101'))

    def test_without_group_code_everything_is_a_person(self):
        """Без кода группы отличить нечем — тогда лучше не терять записи."""
        parser = SocioParser()
        self.assertEqual(len(parser._find_students(self.HTML)), 4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
