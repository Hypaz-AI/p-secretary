"""Contratti offline delle capacità opzionali (flussi e interruttori).

No test falls back to config.DATABASE_URL or invokes connectors/services.
"""
import unittest
from unittest.mock import MagicMock, patch

import capabilities as caps
import flows


class CapabilityTests(unittest.TestCase):
    def connection(self, profile=None, rows=()):
        conn = MagicMock()

        def execute(query, params=None):
            cursor = MagicMock()
            if "to_regclass('secretary_profile')" in query:
                cursor.fetchone.return_value = (profile is not None,)
            elif 'SELECT provenance' in query:
                cursor.fetchone.return_value = (profile,) if profile else None
            elif "to_regclass('flow_settings')" in query:
                cursor.fetchone.return_value = (True,)
            elif query.startswith('SELECT flow,enabled'):
                cursor.fetchall.return_value = list(rows)
            return cursor

        conn.execute.side_effect = execute
        return conn

    def test_registry_is_static_metadata_not_code(self):
        self.assertTrue({'extraction', 'search', 'cataloguing', 'drafts', 'memory',
                         'tasks', 'calendar', 'invoices', 'monthly_zip'} <= caps.REGISTRY.keys())
        self.assertEqual(caps.OPTIONAL, ('invoices', 'monthly_zip'))
        for spec in caps.REGISTRY.values():
            self.assertTrue(all(isinstance(name, str) for name in spec.tools))
        with self.assertRaises(TypeError):
            caps.REGISTRY['shell'] = None

    def test_unconfigured_optional_checks_never_connect(self):
        with patch('db.available', return_value=False), patch('db._conn') as connect:
            self.assertFalse(caps.enabled('invoices'))
            self.assertFalse(caps.enabled('monthly_zip'))
            self.assertTrue(caps.enabled('search'))
        connect.assert_not_called()

    def test_failed_optional_reads_fail_closed(self):
        with patch('db.available', return_value=True), patch('db._conn', side_effect=OSError('offline')):
            self.assertFalse(caps.enabled('invoices'))
        conn = self.connection()
        conn.execute.side_effect = OSError('caller transaction')
        with self.assertRaises(OSError):
            caps.settings(conn=conn)

    def test_fresh_defaults_and_explicit_legacy_flow_before_migration(self):
        for profile in (None, 'fresh'):
            with self.subTest(profile=profile):
                conn = self.connection(profile, [('invoices', True), ('monthly_zip', False)])
                self.assertTrue(caps.enabled('invoices', conn=conn))
                self.assertFalse(caps.enabled('monthly_zip', conn=conn))
                self.assertFalse(caps.enabled('invoices', conn=self.connection(profile)))

    def test_legacy_missing_rows_true_but_explicit_false_wins(self):
        conn = self.connection('legacy', [('invoices', False)])
        values = caps.settings(conn=conn)
        self.assertFalse(values['invoices'])
        self.assertTrue(values['monthly_zip'])

    def test_set_uses_only_authoritative_flow_without_owning_connection(self):
        conn = self.connection()
        with patch('db._conn') as connect:
            caps.set_enabled('invoices', False, conn=conn)
        connect.assert_not_called()
        conn.transaction.assert_not_called()
        conn.commit.assert_not_called()
        conn.close.assert_not_called()
        query, params = conn.execute.call_args.args
        self.assertIn('INSERT INTO flow_settings', query)
        self.assertEqual(params, ('invoices', False))

    def test_unknown_names_and_non_booleans_fail_before_io(self):
        with patch('db._conn') as connect:
            for name, value in [('shell', True), ('search', False), ('invoices', 1),
                                ('invoices', 'true')]:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    caps.set_enabled(name, value)
            with self.assertRaises(ValueError):
                caps.enabled('shell')
            with self.assertRaises(ValueError):
                caps.migrate(MagicMock(), legacy=1)
        connect.assert_not_called()

    def test_flows_init_uses_profile_and_preserves_existing_rows(self):
        for profile, default in [(None, False), ('fresh', False), ('legacy', True)]:
            conn = self.connection(profile)
            with self.subTest(profile=profile), patch('db._conn') as connect:
                connect.return_value.__enter__.return_value = conn
                flows.init()
            inserts = [call.args for call in conn.execute.call_args_list
                       if call.args[0].startswith('INSERT INTO flow_settings')]
            self.assertEqual(len(inserts), len(flows.LABELS))
            for query, (name, value) in inserts:
                self.assertIn('ON CONFLICT DO NOTHING', query)
                self.assertEqual(value, default if name in caps.OPTIONAL else True)

    def test_flows_missing_known_optional_uses_same_defaults(self):
        for profile, default in [(None, False), ('fresh', False), ('legacy', True)]:
            conn = self.connection(profile, [('invoices', False), ('email', False)])
            with self.subTest(profile=profile), patch('db._conn') as connect:
                connect.return_value.__enter__.return_value = conn
                values = flows.settings()
            self.assertFalse(values['email'])
            self.assertFalse(values['invoices'])
            self.assertEqual(values['monthly_zip'], default)
            self.assertTrue(values['digest'])


if __name__ == '__main__':
    unittest.main()