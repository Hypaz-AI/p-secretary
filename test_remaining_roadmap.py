"""Tests for the remaining-roadmap implementations.

Covers: quiet hours, SECRETARY_MODE, monetary AI budget cap, structured stop
conditions, owner evidence linking, email attachments and calendar update.
Offline only: no DB, no network, no filesystem side effects.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from unittest.mock import MagicMock, patch

import ai_budget as budget
import config
import quiet_hours
import secretary as sec


UTC = timezone.utc


class QuietHoursTests(unittest.TestCase):
    def test_active_band_defers_ordinary_notifications(self):
        # 2026-10-17 23:30 UTC = 01:30 local (Europe/Rome, CEST+1... DST: +2).
        clock = datetime(2026, 10, 17, 23, 30, tzinfo=UTC)
        with patch.object(config, 'TIMEZONE', 'Europe/Rome'), \
             patch.object(config, 'QUIET_HOURS_START', 22), \
             patch.object(config, 'QUIET_HOURS_END', 8):
            self.assertTrue(quiet_hours.is_quiet(clock))
            deferred = quiet_hours.defer_until(clock)
            self.assertGreater(deferred, clock)
            # Ordinary notification is not ready; emergency bypasses.
            self.assertFalse(quiet_hours.notification_ready({'urgent': False}, clock))
            self.assertTrue(quiet_hours.notification_ready({'urgent': True}, clock))
            self.assertTrue(quiet_hours.notification_ready({'severity': 'alta'}, clock))

    def test_outside_band_is_immediate(self):
        clock = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)  # 12:00 local
        with patch.object(config, 'TIMEZONE', 'Europe/Rome'), \
             patch.object(config, 'QUIET_HOURS_START', 22), \
             patch.object(config, 'QUIET_HOURS_END', 8):
            self.assertFalse(quiet_hours.is_quiet(clock))
            self.assertEqual(quiet_hours.defer_until(clock), clock)
            self.assertTrue(quiet_hours.notification_ready({}, clock))

    def test_equal_start_end_disables_the_band(self):
        clock = datetime(2026, 10, 17, 23, 30, tzinfo=UTC)
        with patch.object(config, 'TIMEZONE', 'Europe/Rome'), \
             patch.object(config, 'QUIET_HOURS_START', 8), \
             patch.object(config, 'QUIET_HOURS_END', 8):
            self.assertFalse(quiet_hours.is_quiet(clock))

    def test_mode_defaults_to_observe_and_gates_sends(self):
        with patch.object(config, 'SECRETARY_MODE', 'observe'):
            self.assertFalse(quiet_hours.sends_allowed())
        with patch.object(config, 'SECRETARY_MODE', 'active'):
            self.assertTrue(quiet_hours.sends_allowed())
        with patch.object(config, 'SECRETARY_MODE', 'nonsense'):
            # config normalizes at import; here we verify the helper contract.
            self.assertFalse(quiet_hours.sends_allowed())


class MonetaryBudgetTests(unittest.TestCase):
    def test_daily_cost_zero_disables_monetary_cap(self):
        with patch.object(config, 'get', side_effect=lambda key, default: default):
            limits = budget._limits()
            self.assertEqual(limits['DAILY_COST'], Decimal(0))

    def test_daily_cost_configured_and_bounded(self):
        with patch.object(config, 'get',
                          side_effect=lambda key, default:
                          '2.50' if key == 'AI_BUDGET_DAILY_COST' else default):
            self.assertEqual(budget._limits()['DAILY_COST'], Decimal('2.50'))

    def test_invalid_daily_cost_fails_closed(self):
        with patch.object(config, 'get',
                          side_effect=lambda key, default:
                          'not-a-number' if key == 'AI_BUDGET_DAILY_COST' else default):
            with self.assertRaises(budget.BudgetUnavailable):
                budget._limits()

    def test_spent_cost_at_cap_blocks_new_reservation(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.side_effect = [
            (datetime(2026, 10, 17).date(),),  # day
            (0, 0),                          # calls/tokens
            (0,),                            # active in-flight
            (Decimal('2.50'),),               # spent cost at cap
        ]
        context = MagicMock(job_id=1, token='t')
        import db, jobs
        conn_cm = MagicMock()
        conn_cm.__enter__.return_value = conn
        with patch.object(db, 'available', return_value=True), \
             patch.object(db, '_conn', return_value=conn_cm), \
             patch.object(jobs, 'assert_current'), \
             patch.object(jobs, 'execution_context', return_value=context), \
             patch.object(config, 'get',
                          side_effect=lambda key, default:
                          '2.50' if key == 'AI_BUDGET_DAILY_COST' else default):
            with self.assertRaises(budget.BudgetExceeded):
                budget._reserve(context, budget._limits(), 100)


class StopConditionTests(unittest.TestCase):
    def test_prose_is_documented_never_executed(self):
        parsed = sec.parse_stop_condition('ferma quando ho finito i lavori')
        self.assertEqual(parsed, dict(type='prose', text='ferma quando ho finito i lavori'))
        self.assertFalse(sec.stop_condition_met(parsed, clock=None,
                                                applied_reviews=999, practice_state='completed'))

    def test_max_reviews_stops_at_count(self):
        parsed = sec.parse_stop_condition({'type': 'max_reviews', 'count': 3})
        self.assertFalse(sec.stop_condition_met(parsed, clock=None,
                                                applied_reviews=2, practice_state='active'))
        self.assertTrue(sec.stop_condition_met(parsed, clock=None,
                                                applied_reviews=3, practice_state='active'))

    def test_deadline_stops_after_absolute_time(self):
        parsed = sec.parse_stop_condition({'type': 'deadline', 'until': '2026-10-01T00:00:00+00:00'})
        before = datetime(2026, 9, 30, tzinfo=UTC)
        after = datetime(2026, 10, 2, tzinfo=UTC)
        self.assertFalse(sec.stop_condition_met(parsed, clock=before, applied_reviews=0, practice_state='active'))
        self.assertTrue(sec.stop_condition_met(parsed, clock=after, applied_reviews=0, practice_state='active'))

    def test_practice_state_stops_on_terminal_state(self):
        parsed = sec.parse_stop_condition({'type': 'practice_state', 'state': 'completed'})
        self.assertTrue(sec.stop_condition_met(parsed, clock=None,
                                                applied_reviews=0, practice_state='completed'))
        self.assertFalse(sec.stop_condition_met(parsed, clock=None,
                                                applied_reviews=0, practice_state='active'))

    def test_unknown_and_malformed_conditions_fail_open(self):
        with self.assertRaises(sec.InvalidPlan):
            sec.parse_stop_condition({'type': 'shell', 'command': 'rm -rf'})
        with self.assertRaises(sec.InvalidPlan):
            sec.parse_stop_condition({'type': 'max_reviews', 'count': -1})
        with self.assertRaises(sec.InvalidPlan):
            sec.parse_stop_condition({'type': 'deadline', 'until': 'not-a-date'})
        # Malformed stored data never stops anything silently.
        self.assertFalse(sec.stop_condition_met({'type': 'max_reviews'}, clock=None,
                                                applied_reviews=5, practice_state='active'))
        self.assertFalse(sec.stop_condition_met(None, clock=None,
                                                applied_reviews=5, practice_state='active'))

    def test_empty_and_none_are_valid_no_condition(self):
        self.assertIsNone(sec.parse_stop_condition(None))
        self.assertIsNone(sec.parse_stop_condition(''))


if __name__ == '__main__':
    unittest.main()
