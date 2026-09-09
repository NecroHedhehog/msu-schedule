#!/usr/bin/env python3
"""
Тесты окон между парами.

Окно — это не «нет пары», а «пара пропущена посередине дня». Человеку
важно увидеть его заранее: у 44% учебных дней в базе есть хотя бы одно.

Отдельно проверяется случай, из-за которого окно было бы враньём:
выбранный языковой поток показывается отдельным блоком, но пару он
занимает, и окна на этом месте нет.

Запуск:  python -m unittest discover -s tests -t .
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.formatting import (
    pairs_word, format_duration, group_by_pair, format_gap, format_day_schedule,
)

PAIR_TIMES = {
    1: ('09:00', '10:30'),
    2: ('10:40', '12:10'),
    3: ('12:20', '13:50'),
    4: ('14:00', '15:30'),
    5: ('15:40', '17:10'),
    6: ('17:20', '18:50'),
}

MONDAY = date(2026, 9, 7)


def lesson(pair, subject='Социология', teacher='Иванов И.И.',
           subgroup='', room='522', start=None, end=None):
    t_start, t_end = PAIR_TIMES[pair]
    return {
        'pair_number': pair,
        'time_start': start or t_start,
        'time_end': end or t_end,
        'subject': subject,
        'subject_abbr': subject[:3].upper(),
        'lesson_type': 'Лк',
        'room': room,
        'teacher': teacher,
        'subgroup': subgroup,
    }


def render(lessons, choice=None):
    return format_day_schedule(lessons, MONDAY, with_ad=False,
                               stream_choice=choice)


def gap_lines(text):
    return [l.strip() for l in text.split('\n') if '⌛' in l]


class PairsWordTest(unittest.TestCase):
    """Числительное. «окно 1 пар» читается как опечатка, а не как расписание."""

    def test_forms(self):
        self.assertEqual(pairs_word(1), 'пара')
        self.assertEqual(pairs_word(2), 'пары')
        self.assertEqual(pairs_word(4), 'пары')
        self.assertEqual(pairs_word(5), 'пар')


class DurationTest(unittest.TestCase):

    def test_forms(self):
        self.assertEqual(format_duration(45), '45 мин')
        self.assertEqual(format_duration(60), '1 ч')
        self.assertEqual(format_duration(80), '1 ч 20 мин')


class GroupByPairTest(unittest.TestCase):

    def test_sorted_and_merged(self):
        groups = group_by_pair([lesson(4), lesson(1), lesson(4, subject='Экономика')])
        self.assertEqual([p for p, _ in groups], [1, 4])
        self.assertEqual(len(groups[1][1]), 2)


class FormatGapTest(unittest.TestCase):
    """
    Время окна берётся из соседних занятий, а не из таблицы пар: по средам
    четвёртая и пятая идут в другое время, и таблица бы соврала.
    """

    def test_uses_lesson_times(self):
        prev = [lesson(2, end='12:10')]
        nxt = [lesson(4, start='13:30')]
        line = format_gap(prev, nxt, 1)
        self.assertIn('12:10–13:30', line)
        self.assertIn('окно 1 пара', line)

    def test_long_break_without_skipped_pairs(self):
        """
        По средам МФК начинается через час с лишним после третьей пары,
        а номера идут подряд. Пар не пропущено — окно есть.
        """
        line = format_gap([lesson(3, end='13:50')], [lesson(4, start='15:10')], 0)
        self.assertIn('перерыв 1 ч 20 мин', line)
        self.assertIn('13:50–15:10', line)

    def test_normal_break_is_silent(self):
        self.assertIsNone(
            format_gap([lesson(1)], [lesson(2)], 0))
        self.assertIsNone(
            format_gap([lesson(1, end='10:30')], [lesson(2, start='10:50')], 0))

    def test_widest_span_when_pair_has_several_lessons(self):
        prev = [lesson(2, end='12:10'), lesson(2, subject='Экономика', end='12:30')]
        nxt = [lesson(5, start='15:40'), lesson(5, subject='Право', start='15:00')]
        line = format_gap(prev, nxt, 2)
        self.assertIn('12:30–15:00', line)
        self.assertIn('окно 2 пары', line)


class DayGapTest(unittest.TestCase):

    def test_single_gap(self):
        text = render([lesson(2), lesson(4)])
        self.assertEqual(gap_lines(text), ['⌛ <i>окно 1 пара · 12:10–14:00</i>'])

    def test_two_pair_gap(self):
        text = render([lesson(1), lesson(4)])
        self.assertEqual(gap_lines(text), ['⌛ <i>окно 2 пары · 10:30–14:00</i>'])

    def test_consecutive_pairs_have_no_gap(self):
        text = render([lesson(1), lesson(2), lesson(3)])
        self.assertEqual(gap_lines(text), [])

    def test_gap_not_shown_before_first_or_after_last(self):
        """Третьей парой начали, четвёртой кончили — окон нет ни одного."""
        text = render([lesson(3), lesson(4)])
        self.assertEqual(gap_lines(text), [])

    def test_long_break_between_consecutive_pairs(self):
        text = render([lesson(3, end='13:50'),
                       lesson(4, subject='Физкультура', start='15:10', end='16:40')])
        self.assertEqual(gap_lines(text),
                         ['⌛ <i>перерыв 1 ч 20 мин · 13:50–15:10</i>'])

    def test_several_gaps_in_one_day(self):
        text = render([lesson(1), lesson(3), lesson(6)])
        self.assertEqual(len(gap_lines(text)), 2)

    def test_gap_line_sits_between_its_lessons(self):
        text = render([lesson(2, subject='Социология'),
                       lesson(4, subject='Экономика')])
        lines = [l for l in text.split('\n') if l.strip()]
        idx_gap = next(i for i, l in enumerate(lines) if 'окно' in l)
        idx_soc = next(i for i, l in enumerate(lines) if 'СОЦ' in l)
        idx_eco = next(i for i, l in enumerate(lines) if 'ЭКО' in l)
        self.assertLess(idx_soc, idx_gap)
        self.assertLess(idx_gap, idx_eco)

    def test_single_lesson_has_no_gap(self):
        self.assertEqual(gap_lines(render([lesson(3)])), [])

    def test_empty_day(self):
        self.assertEqual(gap_lines(render([])), [])


class StreamGapTest(unittest.TestCase):
    """
    Языковой поток показывается отдельным блоком, но пару занимает.
    Если его не учесть, бот пообещает окно там, где человек сидит
    на английском, — и это худшая из возможных ошибок в расписании.
    """

    def setUp(self):
        self.lessons = [
            lesson(2),
            lesson(3, subject='Английский язык', teacher='Рассошенко Ж.В.',
                   subgroup='мг51соврс-1'),
            lesson(3, subject='Английский язык', teacher='Петрова А.А.',
                   subgroup='мг51соврс-2'),
            lesson(4, subject='Экономика'),
        ]

    def test_chosen_stream_closes_the_gap(self):
        choice = {'subject': 'Английский язык', 'teacher': 'Рассошенко Ж.В.',
                  'subgroup': 'мг51соврс-1'}
        self.assertEqual(gap_lines(render(self.lessons, choice)), [])

    def test_gap_shown_when_nothing_chosen(self):
        """Без выбора языка третья пара человеку не гарантирована — окно есть."""
        self.assertEqual(len(gap_lines(render(self.lessons))), 1)

    def test_gap_shown_when_chosen_stream_is_another_day(self):
        choice = {'subject': 'Французский язык', 'teacher': '', 'subgroup': ''}
        self.assertEqual(len(gap_lines(render(self.lessons, choice))), 1)


if __name__ == '__main__':
    unittest.main()
