"""Offline regressions; production connections and HTTP are forbidden."""
import json
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

import documents
import jobs
import pipeline
import invoices
from connectors import gcal


class DurableTests(unittest.TestCase):
    def test_formatter_import_is_safe_in_worker_thread(self):
        # Fresh interpreter: an already cached main module would hide the bug.
        result = subprocess.run([sys.executable, '-c', '''
import threading
errors = []
def worker():
    try:
        from main import format_verdict
        assert callable(format_verdict)
    except Exception as exc:
        errors.append(repr(exc))
t = threading.Thread(target=worker)
t.start()
t.join()
assert not errors, errors
'''], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_worker_records_sanitized_error_code_without_secrets(self):
        secret = 'token=private-example https://offline.invalid/private-body'
        handler = MagicMock(side_effect=ValueError(secret))
        worker = jobs.Worker('analysis', handler)
        job = (42, {'message_id': 7}, 1, 'token')
        with patch.object(jobs, 'claim', return_value=job), \
             patch.object(jobs, 'finish', side_effect=lambda *args: worker.stop()), \
             patch.object(jobs.logger, 'warning') as log, \
             patch.object(jobs.logger, 'exception') as traceback_log:
            worker.run()
            handler.assert_called_once_with(job[1])
            jobs.finish.assert_called_once_with(job, handler.side_effect)
            log.assert_called_once_with('Work job %s failed (%s)', job[0], 'ValueError')
            traceback_log.assert_not_called()
            self.assertNotIn(secret, str(log.call_args_list))
            self.assertNotIn(secret, jobs.describe_error(handler.side_effect))
        self.assertIsNone(jobs.execution_context())

    def setUp(self):
        self.connect = patch('db._conn')
        self.conn = self.connect.start().return_value.__enter__.return_value
        self.addCleanup(self.connect.stop)
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        exclusion = patch('message_store.reconciliation_excluded', return_value=False)
        exclusion.start()
        self.addCleanup(exclusion.stop)

    def native_source(self, **metadata):
        # Mock archived DB content only; retain the real deterministic policy decision.
        return patch.object(pipeline.policy, 'metadata_for', return_value={
            'id': 42, 'channel': 'email', 'metadata': metadata})

    def test_ingest_commits_source_and_queue_without_ai(self):
        with patch('message_store.store', return_value=42) as store, patch('brain.analyze') as ai, \
             self.native_source(), patch('secretary_runtime.observe') as observe:
            result = pipeline.ingest('email','sender','subject','body',source_id='id')
        self.assertEqual(result['message_id'],42)
        store.assert_called_once()
        ai.assert_not_called()
        observe.assert_called_once_with(self.conn, 42)
        queues = [call for call in self.conn.execute.call_args_list
                  if 'INSERT INTO work_jobs' in call.args[0]]
        self.assertEqual(len(queues), 2)
        sql, params = queues[0].args
        self.assertIn('ON CONFLICT',sql)
        self.assertEqual(params[:2],('analysis','analysis:42'))
        _, reconciliation = queues[1].args
        self.assertEqual(reconciliation[0], 'reconcile')
        self.assertEqual(json.loads(reconciliation[2]), {'message_id': 42, 'revision': ''})

    def test_notification_failure_does_not_mark_sent(self):
        payload = {'type':'notify','message_id':42,'text':'x'}
        # Isolate receipt persistence, not _effect's attempt/result ordering or policy recheck.
        with self.native_source(), patch('connectors.telegram.notify', return_value=False) as send, \
             patch.object(pipeline.policy, 'prepare_effect', return_value=True), \
             patch.object(pipeline.policy, 'record_receipt', return_value=True) as receipt:
            with self.assertRaises(pipeline.EffectUncertain):
                pipeline.action(payload)
        send.assert_called_once_with('x', html=True)
        self.assertEqual([call.args[2] for call in receipt.call_args_list], ['attempted', 'uncertain'])
        self.assertTrue(all(call.args[1] == pipeline._notification_key(payload)
                            for call in receipt.call_args_list))
        self.assertFalse(any('UPDATE message_analysis' in call.args[0]
                             for call in self.conn.execute.call_args_list))

    def test_notification_without_native_source_is_blocked_before_send(self):
        self.conn.execute.return_value.fetchone.return_value = None
        with patch('connectors.telegram.notify') as send:
            # A policy denial is monotonic: the job fails permanently and
            # visibly instead of looping on Deferred forever.
            with self.assertRaisesRegex(jobs.Permanent, 'Effect policy denied'):
                pipeline.action({'type':'notify','message_id':42,'text':'x'})
        send.assert_not_called()
        self.assertFalse(any('UPDATE message_analysis' in call.args[0]
                             or 'INSERT INTO effect_receipts' in call.args[0]
                             for call in self.conn.execute.call_args_list))
        audit = next(call for call in self.conn.execute.call_args_list
                     if 'INSERT INTO effect_audit' in call.args[0])
        self.assertEqual(audit.args[1], (42, 'notify', False, 'missing_message'))

    def test_proposal_notification_uses_evidence_provenance_not_notified_flag(self):
        # Proposal notifications carry evidence_message_id as their native
        # authority; they must never mark a verdict notification as delivered.
        payload = {'type':'notify','text':'x','html':False,'evidence_message_id':42}
        with self.native_source(), patch('connectors.telegram.notify', return_value=True) as send, \
             patch.object(pipeline.policy, 'prepare_effect', return_value=True), \
             patch.object(pipeline.policy, 'record_receipt', return_value=True):
            pipeline.action(payload)
        send.assert_called_once_with('x', html=False)
        self.assertFalse(any('UPDATE message_analysis' in call.args[0]
                             for call in self.conn.execute.call_args_list))

    def test_reminder_without_native_origin_fails_permanently(self):
        # A task without any native origin can never pass the notify gate.
        # reminder_is_current passes, the task row exists (deliverable in
        # principle), but the origin lookup returns nothing: permanent failure.
        with patch('task_reconciliation.reminder_is_current', return_value=True):
            self.conn.execute.return_value.fetchone.side_effect = [
                ('Titolo attività',), None]  # task row, then origin lookup
            with patch('connectors.telegram.notify') as send:
                with self.assertRaisesRegex(jobs.Permanent, 'provenienza nativa'):
                    pipeline.action({'type': 'reminder', 'id': 7, 'deadline': '2099-01-01T00:00:00+00:00'})
            send.assert_not_called()

    def test_task_proposal_commands_use_persistent_owner_handler(self):
        with patch('task_reconciliation.owner_commands', return_value=True) as owner:
            self.assertTrue(pipeline.owner_commands('/proposte'))
        owner.assert_called_once_with('/proposte')
        self.conn.execute.assert_not_called()

    def test_manual_done_uses_lifecycle_cleanup(self):
        with patch('task_reconciliation.close_task', return_value=True) as close, \
             patch('connectors.telegram.notify') as notify:
            self.assertTrue(pipeline.owner_commands('/fatto 7'))
        close.assert_called_once_with(self.conn, 7)
        self.assertEqual(notify.call_args.args[0], 'Attività completata.')

    def test_stale_reminder_is_not_delivered(self):
        with patch('task_reconciliation.reminder_is_current', return_value=False) as gate, \
             patch('connectors.telegram.notify') as notify:
            pipeline.action({'type': 'reminder', 'id': 7})
        gate.assert_called_once_with(self.conn, {'type': 'reminder', 'id': 7})
        notify.assert_not_called()

    def test_scheduler_stamps_current_deadline(self):
        from datetime import datetime, timezone
        deadline = datetime(2099, 1, 1, tzinfo=timezone.utc)
        # The first SELECT is for owner reminders, the second for legacy deadline reminders.
        self.conn.execute.return_value.fetchall.side_effect = [[], [(7, deadline)]]
        with patch('flows.enabled', return_value=False), patch('jobs.enqueue') as enqueue, \
             patch('secretary.due_reviews') as reviews, patch('secretary_runtime.backlog') as backlog:
            pipeline.schedule()
        self.assertEqual(enqueue.call_args.args[3], {
            'type': 'reminder', 'id': 7, 'deadline': deadline.isoformat()})
        reviews.assert_called_once_with(self.conn)
        backlog.assert_called_once_with(self.conn)

    def test_effect_audit_retention_prunes_once_per_day(self):
        # Retention is guarded by a source_cursors day stamp: one DELETE per
        # day, disabled at EFFECT_AUDIT_RETENTION_DAYS=0. Receipts untouched.
        # First schedule(): no stamp -> prune + stamp today. Second: the
        # stamped day matches the real current day -> skip.
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        self.conn.execute.return_value.fetchone.side_effect = [None, ({'day': today},)]
        with patch('flows.enabled', return_value=False), \
             patch('secretary.due_reviews'), patch('secretary_runtime.backlog'):
            with patch.object(pipeline.config, 'EFFECT_AUDIT_RETENTION_DAYS', 30):
                pipeline.schedule()
                pipeline.schedule()
        prunes = [call for call in self.conn.execute.call_args_list
                  if 'DELETE FROM effect_audit' in call.args[0]]
        self.assertEqual(len(prunes), 1)
        self.assertIn('interval', prunes[0].args[0])
        self.assertEqual(prunes[0].args[1], (30,))
        stamps = [call for call in self.conn.execute.call_args_list
                  if 'effect_audit_retention_day' in call.args[0]]
        self.assertEqual(len(stamps), 3)  # read + upsert, then read again
        self.conn.execute.reset_mock()
        with patch('flows.enabled', return_value=False), \
             patch('secretary.due_reviews'), patch('secretary_runtime.backlog'), \
             patch.object(pipeline.config, 'EFFECT_AUDIT_RETENTION_DAYS', 0):
            pipeline.schedule()
        self.assertFalse(any('DELETE FROM effect_audit' in call.args[0]
                             for call in self.conn.execute.call_args_list))

    def test_notification_ack_marks_actual_sent(self):
        with self.native_source(), patch('connectors.telegram.notify', return_value=True) as send, \
             patch.object(pipeline.policy, 'prepare_effect', return_value=True), \
             patch.object(pipeline.policy, 'record_receipt', return_value=True) as receipt:
            pipeline.action({'type':'notify','message_id':42,'text':'x'})
        send.assert_called_once_with('x', html=True)
        self.assertEqual([call.args[2] for call in receipt.call_args_list], ['attempted', 'accepted'])
        self.assertIn('notified=TRUE', self.conn.execute.call_args.args[0])
        self.assertEqual(self.conn.execute.call_args.args[1], (42,))

    def test_retry_backoff_and_token_fencing(self):
        jobs.finish((1,{},3,'lease-token'), TimeoutError('token=private-example'))
        sql, args = self.conn.execute.call_args.args
        self.assertIn('token=%s', sql)
        self.assertEqual(args,('pending','TimeoutError','[redacted]',False,60,1,'lease-token'))

    def test_claim_uses_skip_locked_and_expiry(self):
        self.conn.execute.return_value.fetchone.return_value = (1,{},1)
        self.assertEqual(jobs.claim('analysis')[:3],(1,{},1))
        sql = self.conn.execute.call_args_list[0].args[0]
        self.assertIn('SKIP LOCKED',sql)
        self.assertIn('lease_until<now()',sql)

    def test_xml_entities_and_encoding_rejected(self):
        for encoding in ('utf-8','utf-16','utf-32'):
            with self.assertRaises(ValueError):
                documents.extract('<!DOCTYPE x [<!ENTITY e "bad">]><x>&e;</x>'.encode(encoding),'x.xml')
        self.assertEqual(documents.extract(b'<invoice><number>123</number></invoice>','x.xml'),'123')

    def test_document_size_bounded(self):
        with self.assertRaises(ValueError):
            documents.extract(b'x'*(documents.MAX_BYTES+1),'x.xml')

    def test_calendar_identity_stable_and_conflict_recovered(self):
        identity = jobs.event_id('source:42')
        self.assertRegex(identity,r'^[0-9a-f]{64}$')
        with patch.object(gcal,'validate_event', return_value={'dt':'2099-01-01 10:00','duration_min':60,'reminder_minutes':30}), \
               patch.object(gcal,'_cal_path', return_value='offline-shared-calendar'), \
             patch.object(gcal,'_api',side_effect=[RuntimeError('Google Calendar API 409: conflict'),{'id':identity}]):
            result = gcal.create_event('X','2099-01-01 10:00',event_id=identity)
        self.assertTrue(result['ok'])

    def test_merge_keeps_pending_and_deduplicates_remote(self):
        remote = [{'event_id':'a','dt':'2099-01-01','title':'Remote'}]
        local = [{'event_id':'a','dt':'2099-01-01'}, {'event_id':'b','dt':'2099-01-02','sync_state':'pending'}]
        self.assertEqual(len(pipeline.merge_agenda(remote,local)),2)

    def test_proposal_does_not_enqueue_external_write(self):
        self.conn.execute.return_value.fetchone.return_value=(4,)
        with patch.object(jobs,'enqueue') as enqueue:
            pipeline.save_event(self.conn,{'title':'X','dt':'2099-01-01 10:00'},'key','email',False)
        enqueue.assert_not_called()
        self.assertIn('proposal', self.conn.execute.call_args.args[1])

    def test_analysis_failure_remains_retryable(self):
        def execute(sql, params=None):
            cursor = MagicMock()
            cursor.fetchone.return_value = (True,) if 'SELECT allowed FROM data_policies' in sql else None
            return cursor
        self.conn.execute.side_effect = execute
        with patch.object(pipeline,'load_message',return_value=(('email','x','s','body','id'),[])), \
             self.native_source(), \
             patch('flows.enabled',return_value=True), \
             patch('brain.analyze',return_value={'category':'errore'}) as ai:
            with self.assertRaisesRegex(RuntimeError, 'Analysis unavailable'):
                pipeline.analyze({'message_id':42})
        ai.assert_called_once()
        self.assertFalse(any('INSERT INTO message_analysis' in c.args[0] for c in self.conn.execute.call_args_list))

    def test_analysis_without_explicit_data_grant_is_deferred_before_ai(self):
        self.conn.execute.return_value.fetchone.return_value = None
        with self.native_source(), patch.object(pipeline, 'load_message', return_value=(
                ('email', 'x', 's', 'body', 'id'), [])), patch('flows.enabled', return_value=True), \
             patch('brain.analyze') as ai:
            with self.assertRaisesRegex(jobs.Deferred, 'AI text data policy denied'):
                pipeline.analyze({'message_id': 42})
        ai.assert_not_called()
        audit = [call.args[1] for call in self.conn.execute.call_args_list
                 if 'INSERT INTO effect_audit' in call.args[0]]
        self.assertIn((42, 'ai_text', False, 'data_denied'), audit)
        self.assertFalse(any('INSERT INTO message_analysis' in c.args[0] for c in self.conn.execute.call_args_list))

    def test_closed_month_catchup_never_schedules_current_month(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        current = datetime.now(ZoneInfo(pipeline.config.TIMEZONE)).strftime('%Y-%m')
        self.conn.execute.return_value.fetchall.return_value=[]
        with patch('flows.enabled',return_value=True), patch('db.months_with_invoices',return_value=[
            {'entity':'a','month':'2000-01'}, {'entity':'b','month':'2000-02'},
            {'entity':'a','month':current}]), patch('jobs.submit') as submit, \
             patch.object(pipeline.capabilities, 'enabled', return_value=True), \
             patch('secretary.due_reviews'), patch('secretary_runtime.backlog'):
            pipeline.schedule()
        self.assertEqual([c.args[1] for c in submit.call_args_list],['zip:2000-01','zip:2000-02'])

    def test_invoice_hash_duplicate_reuses_existing_file(self):
        from pathlib import Path
        import tempfile
        self.conn.execute.return_value.fetchone.return_value=('/existing/invoice.xml',)
        with tempfile.TemporaryDirectory() as folder, patch.object(invoices.config,'INVOICE_DIR',Path(folder)):
            result = invoices.save_invoice('unknown','x.xml',b'<invoice/>')
            self.assertEqual(result,Path('/existing/invoice.xml'))
            self.assertEqual(list(Path(folder).rglob('*.xml')),[])

    def test_paused_job_defer_preserves_attempt_budget(self):
        jobs.defer((1,{},7,'token'))
        sql,args = self.conn.execute.call_args.args
        self.assertIn('attempts-1',sql)
        self.assertEqual(args,(1,'token'))


if __name__ == '__main__':
    unittest.main()