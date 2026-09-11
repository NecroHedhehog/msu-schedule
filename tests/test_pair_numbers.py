#!/usr/bin/env python3
"""
Номер пары берётся из разметки, а не из позиции ячейки.

Находка 12.09.2026, по жалобе подписчика мг54САГУсд: бот звал его
к первой паре, хотя на сайте занятие стоит на второй. Сайт выбрасывает
пустые верхние строки — у этой группы строки первой пары нет вовсе,
и `pair = i + 1` сдвигал весь день на пару вверх. Занятия оставались
на месте, время становилось чужим, и заметить это было нечем.

Риск был описан в docs/SITE.md §7 и docs/TODO.md §7 как «пока
не сработало ни разу». Сработало.

Разметка ниже снята с живой страницы ?gr=470&pMns=9.2026, обрезана
до линейки пар и одной колонки дня. Сети тесты не требуют.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parsers.socio import SocioParser


# Понедельник 14.09.2026 у мг54САГУсд: линейка 2..6, пятая пара пустая
REAL_ROW = (
    '<table><tr><td height="50" width="15%"><table align="center" border="0" bordercolor="#FF0000" cellpadding="0" cellspacing="0" class="TEXT1" height="100%" width="100%"><tr><td align="center" bgcolor="#ABC1C9" colspan="2"></td></tr><tr><td bgcolor="#ABC1C9" title="10.40-12.10" width="1%">2</td><td bgcolor="#ABC1C9" class="TmTblC" height="50" width="100%"><div class="AREATXT"><br/><br/></div></td></tr><tr><td bgcolor="#ABC1C9" title="12.20-13.50" width="1%">3</td><td bgcolor="#ABC1C9" class="TmTblC" height="50" width="100%"><div class="AREATXT"><br/><br/></div></td></tr><tr><td bgcolor="#ABC1C9" title="14.00-15.30" width="1%">4</td><td bgcolor="#ABC1C9" class="TmTblC" height="50" width="100%"><div class="AREATXT"><br/><br/></div></td></tr><tr><td bgcolor="#ABC1C9" title="15.40-17.10" width="1%">5</td><td bgcolor="#ABC1C9" class="TmTblC" height="50" width="100%"><div class="AREATXT"><br/><br/></div></td></tr><tr><td bgcolor="#ABC1C9" title="17.20-18.50" width="1%">6</td><td bgcolor="#ABC1C9" class="TmTblC" height="50" width="100%"><div class="AREATXT"><br/><br/></div></td></tr></table></td><td height="50" width="15%"><table align="center" border="0" bordercolor="#FF0000" cellpadding="0" cellspacing="0" class="TEXT1" height="100%" width="100%"><tr><td align="center" bgcolor="#ABC1C9" colspan="2">14.09.2026</td></tr><tr><td class="TmTblC" height="50" width="100%"><div class="AREATXT1" onclick="DivHint(this)"><div id="LESS" title="Семинар по \'Современные социологические теории и школы\'"><font color="#004000"><b>ССТШк</b></font><br/><b>415</b>[<font color="#766F0C">Сем</font>]<br/></div></div></td></tr><tr><td class="TmTblC" height="50" width="100%"><div class="AREATXT1" onclick="DivHint(this)"><div id="LESS" title="Семинар по \'Методология и история науки\'"><font color="#004000"><b>МИН</b></font><br/><b>406</b>[<font color="#766F0C">Сем</font>]<br/></div></div></td></tr><tr><td class="TmTblC" height="50" width="100%"><div class="AREATXT1" onclick="DivHint(this)"><div id="LESS" title="Лекция по \'Философия\'"><font color="#004000"><b>Филос</b></font><br/><b>522</b>[<font color="#8D2183">Лк</font>]<br/></div></div></td></tr><tr><td class="TmTblC" height="50" width="100%"><div class="AREATXT"><br/><br/></div></td></tr><tr><td class="TmTblC" height="50" width="100%"><div class="AREATXT1" onclick="DivHint(this)"><div id="LESS" title="Семинар по \'Педагогика и психология высшей школы\'"><font color="#004000"><b>ПиПВШ</b></font><br/><b>301</b>[<font color="#766F0C">Сем</font>]<br/></div></div></td></tr></table></td></tr></table>'
)


class TestPairNumberFromMarkup(unittest.TestCase):
    """Главное: пары читаются как 2,3,4,6, а не как 1,2,3,5."""

    def setUp(self):
        self.parser = SocioParser()

    def lessons(self, html=REAL_ROW):
        return sorted(self.parser._parse_page(html),
                      key=lambda l: l['pair_number'])

    def test_pairs_match_the_site(self):
        got = [l['pair_number'] for l in self.lessons()]
        self.assertEqual(got, [2, 3, 4, 6],
                         "день снова уехал: пары считаются по позиции ячейки")

    def test_times_follow_the_pairs(self):
        by_pair = {l['pair_number']: (l['time_start'], l['time_end'])
                   for l in self.lessons()}
        self.assertEqual(by_pair[2], ('10:40', '12:10'))
        self.assertEqual(by_pair[6], ('17:20', '18:50'))

    def test_nobody_is_called_to_the_first_pair(self):
        """Та самая жалоба: девятичасовой пары у этой группы нет."""
        self.assertNotIn(1, [l['pair_number'] for l in self.lessons()])

    def test_empty_slot_does_not_shift_the_rest(self):
        """Пятая пара пустая — шестая обязана остаться шестой."""
        self.assertIn(6, [l['pair_number'] for l in self.lessons()])
        self.assertNotIn(5, [l['pair_number'] for l in self.lessons()])

    def test_ruler_is_found(self):
        self.lessons()
        self.assertEqual(self.parser.days_without_ruler, 0)


class TestRulerFallback(unittest.TestCase):
    """Если линейки нет, считаем по позиции — но громко, а не молча."""

    def setUp(self):
        self.parser = SocioParser()

    @staticmethod
    def day(cells_html, ruler=''):
        cells = ''.join(f'<tr><td class="TmTblC">{c}</td></tr>' for c in cells_html)
        return (f'<table><tr>{ruler}'
                f'<td><table><tr><td>14.09.2026</td></tr>{cells}</table></td>'
                f'</tr></table>')

    @staticmethod
    def lesson(abbr):
        return (f"<div id=\"LESS\" title=\"Семинар по '{abbr}'\">"
                f'<font color="#004000"><b>{abbr}</b></font><br>'
                f'<b>101</b>[<font color="#8D2183">Сем</font>]<br></div>')

    def test_without_ruler_falls_back_and_counts(self):
        html = self.day([self.lesson('А'), self.lesson('Б')])
        got = [l['pair_number'] for l in self.parser._parse_page(html)]
        self.assertEqual(sorted(got), [1, 2])
        self.assertEqual(self.parser.days_without_ruler, 1,
                         "отказ от линейки обязан попасть в счётчик")
        self.assertTrue(self.parser.ruler_samples)

    def test_mismatched_ruler_is_refused(self):
        """Линейка короче дня — соответствие не гарантировано, не угадываем."""
        ruler = ('<td><table>'
                 '<tr><td title="10.40-12.10">2</td><td class="TmTblC"></td></tr>'
                 '</table></td>')
        html = self.day([self.lesson('А'), self.lesson('Б')], ruler)
        self.parser._parse_page(html)
        self.assertEqual(self.parser.days_without_ruler, 1)

    def test_counter_reaches_the_report(self):
        self.parser._parse_page(self.day([self.lesson('А')]))
        self.assertIn('дней без линейки пар: 1', self.parser.stats_line())


if __name__ == '__main__':
    unittest.main()
