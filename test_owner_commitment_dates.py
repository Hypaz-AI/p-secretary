"""Offline deterministic grounding of untrusted owner commitment dates."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import owner_commitments as oc


ANCHOR = datetime(2025, 12, 31, 23, 30, tzinfo=timezone.utc)
TASK = dict(id=7, version=3, title='Contratto', deadline=None,
            owner_deadline_date=None, owner_remind_at=None)


def change(text, fields, operation='create'):
    return dict(operation=operation, task_id=None if operation == 'create' else 7,
                changes=dict(title='Contratto', **fields) if operation == 'create' else fields,
                evidence=text, kind='task', owner_request=True,
                certainty='certain', unique_match=True)


class DateGroundingTests(unittest.TestCase):
    def setUp(self):
        timezone_patch = patch.object(oc.config, 'TIMEZONE', 'Europe/Rome')
        timezone_patch.start()
        self.addCleanup(timezone_patch.stop)

    def prepare(self, text, fields, *, anchor=ANCHOR, operation='create'):
        result = dict(changes=[change(text, fields, operation)], clarification='')
        with patch.object(oc, 'extract', return_value=result):
            return oc.prepare(text, [], anchor, tasks=[TASK] if operation != 'create' else [])

    def test_supported_relative_dates_use_original_local_calendar(self):
        for reference, expected in (
                ('oggi', '2026-01-01'), ('domani', '2026-01-02'),
                ('dopodomani', '2026-01-03'), ('ieri', '2025-12-31'),
                ("l'altro ieri", '2025-12-30'), ('l’altro ieri', '2025-12-30'),
                ('tra 3 giorni', '2026-01-04'), ('fra 0 giorni', '2026-01-01'),
                ('DOMANI', '2026-01-02')):
            text = f'Devo inviare il contratto {reference}.'
            with self.subTest(reference=reference):
                plan = self.prepare(text, {'owner_deadline_date': expected})
                self.assertEqual(plan[0]['changes']['owner_deadline_date'], expected)
                with self.assertRaises(oc.Clarification):
                    self.prepare(text, {'owner_deadline_date': '2099-01-01'})

    def test_year_rollover_and_iso_original_timestamp(self):
        plan = self.prepare('Devo inviare domani.', {'owner_deadline_date': '2026-01-01'},
                            anchor='2025-12-31T21:30:00+00:00')
        self.assertEqual(plan[0]['changes']['owner_deadline_date'], '2026-01-01')

    def test_configured_timezone_not_utc_or_processing_time(self):
        with patch.object(oc.config, 'TIMEZONE', 'America/Los_Angeles'):
            self.prepare('Devo inviare oggi.', {'owner_deadline_date': '2025-12-31'})
            with self.assertRaises(oc.Clarification):
                self.prepare('Devo inviare oggi.', {'owner_deadline_date': '2026-01-01'})

    def test_explicit_dates_include_past_and_leap_day(self):
        for reference, expected in (('2020-01-01', '2020-01-01'),
                                    ('29/02/2024', '2024-02-29')):
            text = f'Devo inviare entro {reference}.'
            with self.subTest(reference=reference):
                self.prepare(text, {'owner_deadline_date': expected})
                with self.assertRaises(oc.Clarification):
                    self.prepare(text, {'owner_deadline_date': '2099-01-01'})

    def test_invalid_or_overflowing_references_need_clarification(self):
        for reference in ('2026-02-30', '31/02/2026', 'tra 999999999999999 giorni'):
            with self.subTest(reference=reference), self.assertRaises(oc.Clarification):
                self.prepare(f'Devo inviare {reference}.', {'owner_deadline_date': '2099-01-01'})

    def test_update_nonnull_date_is_checked(self):
        text = 'Sposta il contratto a domani.'
        self.prepare(text, {'owner_deadline_date': '2026-01-02'}, operation='update')
        with self.assertRaises(oc.Clarification):
            self.prepare(text, {'owner_deadline_date': '2099-01-01'}, operation='update')

    def test_null_and_omitted_dates_do_not_require_parsing(self):
        for fields in ({'deadline': None}, {'owner_deadline_date': None},
                       {'owner_remind_at': None}, {'title': 'Nuovo contratto'}):
            with self.subTest(fields=fields):
                self.prepare('Modifica il contratto previsto oggi o domani.', fields,
                             operation='update')

    def test_conflicting_references_are_ambiguous_but_equivalent_are_not(self):
        for reference in ('oggi o domani', 'domani oppure 04/01/2026'):
            with self.subTest(reference=reference), self.assertRaises(oc.Clarification):
                self.prepare(f'Devo inviare {reference}.', {'owner_deadline_date': '2026-01-02'})
        self.prepare('Devo inviare domani, 02/01/2026 (2026-01-02).',
                     {'owner_deadline_date': '2026-01-02'})

    def test_independent_items_use_own_evidence_not_entire_message(self):
        first = 'Devo inviare oggi.'
        second = 'Devo pagare domani.'
        items = [change(first, {'owner_deadline_date': '2026-01-01'}),
                 change(second, {'owner_deadline_date': '2026-01-02'})]
        items[1]['changes']['title'] = 'Pagamento'
        for separator in (' ', '\n', '; '):
            # A semicolon is part of the first evidence sentence in this guard.
            items[0]['evidence'] = first if separator != '; ' else first + ';'
            with self.subTest(separator=separator), patch.object(
                    oc, 'extract', return_value=dict(changes=items, clarification='')):
                self.assertEqual(len(oc.prepare(first + separator + second, [], ANCHOR, tasks=[])), 2)

    def test_weekdays_and_other_unsupported_expressions_remain_model_dependent(self):
        for reference in ('venerdì', 'la prossima settimana', 'tra due giorni'):
            with self.subTest(reference=reference):
                self.prepare(f'Devo inviare {reference}.', {'owner_deadline_date': '2099-01-01'})

    def test_timed_deadline_validated_without_changing_dst_checks(self):
        text = 'Devo inviare domani alle 15:00.'
        anchor = '2026-03-28T12:00:00+01:00'
        self.prepare(text, {'deadline': '2026-03-29T15:00:00+02:00'}, anchor=anchor)
        with self.assertRaises(oc.Clarification):
            self.prepare(text, {'deadline': '2099-03-29T15:00:00+02:00'}, anchor=anchor)
        with self.assertRaises(ValueError):
            self.prepare(text, {'deadline': '2026-03-29T15:00:00+01:00'}, anchor=anchor)
        with self.assertRaises(ValueError):
            self.prepare('Devo inviare domani alle 02:30.',
                         {'deadline': '2026-03-29T02:30:00+01:00'}, anchor=anchor)

    def test_reminder_only_is_grounded(self):
        text = 'Ricordami di pagare domani alle 15:00.'
        self.prepare(text, {'owner_remind_at': '2026-01-02T15:00:00+01:00'})
        with self.assertRaises(oc.Clarification):
            self.prepare(text, {'owner_remind_at': '2099-01-02T15:00:00+01:00'})

    def test_inline_reminder_word_does_not_hide_deadline_reference(self):
        text = 'Devo ricordarmi di pagare domani.'
        self.prepare(text, {'owner_deadline_date': '2026-01-02'})
        with self.assertRaises(oc.Clarification):
            self.prepare(text, {'owner_deadline_date': '2099-01-01'})

    def test_separate_reminder_does_not_conflict_with_deadline(self):
        for reference, deadline in (('03/01/2026', '2026-01-03'),
                                    ('venerdì', '2099-01-02')):
            text = f'Devo pagare, scadenza {reference}, avvisami domani alle 15:00.'
            fields = dict(owner_deadline_date=deadline,
                          owner_remind_at='2026-01-02T15:00:00+01:00')
            with self.subTest(reference=reference):
                self.prepare(text, fields)
                self.prepare(text, {'owner_remind_at': fields['owner_remind_at']})
        with self.assertRaises(oc.Clarification):
            self.prepare('Devo pagare entro dopodomani, avvisami domani alle 15:00.',
                         {'owner_deadline_date': '2099-01-01'})

    def test_leading_reminder_and_distinct_deadline(self):
        text = 'Ricordami domani alle 15:00 di pagare entro dopodomani.'
        self.prepare(text, dict(owner_deadline_date='2026-01-03',
                                owner_remind_at='2026-01-02T15:00:00+01:00'))

    def test_negated_sentence_is_not_lexically_rejected(self):
        # Intent is the model's judgement now: a negated sentence passes the
        # structural guard (it is still an exact current owner sentence) and
        # the date grounding still validates the extracted date.
        plan = self.prepare('Non devo inviare domani.', {'owner_deadline_date': '2026-01-02'})
        self.assertEqual(plan[0]['changes']['owner_deadline_date'], '2026-01-02')
        with self.assertRaises(oc.Clarification):
            self.prepare('Non devo inviare domani.', {'owner_deadline_date': '2099-01-01'})


if __name__ == '__main__':
    unittest.main()