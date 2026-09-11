#!/usr/bin/env python3
"""
Тесты подавления лишних алертов.

Поводом стал реальный поток: за сутки администратору пришло девять
сообщений, из них ни одного полезного — четыре об успешных прогонах,
три одинаковых предупреждения про одни и те же пустые группы и два
ложных «устарели» у проходов, которые просто ходят реже других.
Сети не требуют.
"""

import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import _create_tables, claim_alert
from check_freshness import max_hours_for, DEFAULT_MAX_HOURS
from core.config import FRESHNESS_MAX_HOURS, FRESHNESS_SKIP


class TestClaimAlert(unittest.TestCase):
    """Один и тот же текст не должен уходить по три раза в день."""

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        _create_tables(self.conn)
        self.addCleanup(self.conn.close)

    def claim(self, text, hours=24, kind='parse_warning', key='socio'):
        return claim_alert(self.conn, kind, key, text, hours)

    def test_first_one_goes_through(self):
        self.assertTrue(self.claim('мало занятий у 4 групп'))

    def test_same_text_is_held_back(self):
        self.assertTrue(self.claim('мало занятий у 4 групп'))
        self.assertFalse(self.claim('мало занятий у 4 групп'),
                         "повтор того же предупреждения ушёл второй раз")

    def test_changed_text_goes_through_at_once(self):
        """Список групп изменился — это новость, ждать суток не надо."""
        self.assertTrue(self.claim('мало занятий у 4 групп: с107, пп202'))
        self.assertTrue(self.claim('мало занятий у 5 групп: с107, пп202, с301'))

    def test_repeats_after_the_window(self):
        self.assertTrue(self.claim('мало занятий'))
        self.conn.execute(
            "UPDATE alert_log SET created_at = datetime('now', '-25 hours')")
        self.conn.commit()
        self.assertTrue(self.claim('мало занятий'), "через сутки надо напомнить")

    def test_different_passes_do_not_shadow_each_other(self):
        self.assertTrue(self.claim('нет данных', key='socio'))
        self.assertTrue(self.claim('нет данных', key='socio-teachers'),
                        "предупреждение другого прохода подавлено чужим")

    def test_kinds_are_independent(self):
        self.assertTrue(self.claim('устарели', kind='stale'))
        self.assertTrue(self.claim('устарели', kind='parse_warning'))


class TestFreshnessThresholds(unittest.TestCase):
    """
    Пороги свежести у проходов разные, потому что и ходят они по-разному.
    С общим порогом в 8 часов суточный проход «устаревал» каждую ночь.
    """

    def test_each_pass_has_its_own(self):
        self.assertEqual(max_hours_for('socio'), FRESHNESS_MAX_HOURS['socio'])
        self.assertEqual(max_hours_for('socio-teachers'),
                         FRESHNESS_MAX_HOURS['socio-teachers'])

    def test_teachers_survive_the_weekend(self):
        """Пн/Ср/Пт: от пятницы до понедельника 72 часа, порог должен быть больше."""
        self.assertGreater(max_hours_for('socio-teachers'), 72)

    def test_daily_pass_survives_a_day(self):
        self.assertGreater(max_hours_for('socio-subgroups'), 24)

    def test_socio_survives_the_night(self):
        """Прогоны в 07:10 и 19:10 — ночью разрыв 12 часов."""
        self.assertGreater(max_hours_for('socio'), 12)

    def test_unknown_pass_falls_back(self):
        self.assertEqual(max_hours_for('какой-то-новый'), DEFAULT_MAX_HOURS)

    def test_explicit_override_wins(self):
        self.assertEqual(max_hours_for('socio', 5), 5)

    def test_manual_pass_is_not_watched(self):
        self.assertIn('socio-students', FRESHNESS_SKIP)


class TestSuccessIsSilent(unittest.TestCase):
    """Успешный прогон по умолчанию молчит: он и так виден в parse_log."""

    def test_silent_by_default(self):
        with mock.patch('core.alerts.ALERT_ON_SUCCESS', False), \
             mock.patch('core.alerts.send_admin_alert') as send:
            from core.alerts import alert_parse_ok
            alert_parse_ok('socio', 46, 5724)
            send.assert_not_called()

    def test_can_be_switched_on(self):
        with mock.patch('core.alerts.ALERT_ON_SUCCESS', True), \
             mock.patch('core.alerts.send_admin_alert') as send:
            from core.alerts import alert_parse_ok
            alert_parse_ok('socio', 46, 5724)
            send.assert_called_once()


class TestErrorsAreNeverHeldBack(unittest.TestCase):
    """Ошибку глушить нельзя ни при каких условиях: её ждут."""

    def test_error_has_no_dedup(self):
        with mock.patch('core.alerts.send_admin_alert') as send:
            from core.alerts import alert_parse_error
            alert_parse_error('socio', 'FetchError на главной')
            alert_parse_error('socio', 'FetchError на главной')
            self.assertEqual(send.call_count, 2)


if __name__ == '__main__':
    unittest.main()
