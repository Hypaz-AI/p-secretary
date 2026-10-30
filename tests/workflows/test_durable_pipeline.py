"""Offline regressions; production connections and HTTP are forbidden."""
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

from secretary.archive import documents
from secretary.core import jobs
from secretary.workflows import pipeline, invoices
from secretary.connectors import gcal


class DurableTests(unittest.TestCase):
    def test_worker_handlers_import_is_safe_in_worker_thread(self):
        # Fresh interpreter: an already cached main module would hide the bug.
        result = subprocess.run([sys.executable, '-c', '''
import signal, threading
before = signal.getsignal(signal.SIGTERM)
errors = []
def worker():
    try:
        from secretary import main
        assert callable(main._owner_job) and callable(main._analysis_job)
    except Exception as exc:
        errors.append(repr(exc))
t = threading.Thread(target=worker)
t.start()
t.join()
assert not errors, errors
assert signal.getsignal(signal.SIGTERM) == before, 'importing main registered a signal handler'
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
        self.connect = patch('secretary.core.db._conn')
        self.conn = self.connect.start().return_value.__enter__.return_value
        self.addCleanup(self.connect.stop)
        self.network = patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def native_source(self, **metadata):
        # Mock archived DB content only; retain the real deterministic policy decision.
        return patch.object(pipeline.policy, 'metadata_for', return_value={
            'id': 42, 'channel': 'email', 'metadata': metadata})

    def test_ingest_commits_source_and_queue_without_ai(self):
        with patch('secretary.archive.message_store.store', return_value=42) as store, self.native_source():
            result = pipeline.ingest('email','sender','subject','body',source_id='id')
        self.assertEqual(result['message_id'],42)
        store.assert_called_once()
        queues = [call for call in self.conn.execute.call_args_list
                  if 'INSERT INTO work_jobs' in call.args[0]]
        # Un solo job durevole: la valutazione la fa l'agente quando lo prende in carico.
        self.assertEqual(len(queues), 1)
        sql, params = queues[0].args
        self.assertIn('ON CONFLICT',sql)
        self.assertEqual(params[:2],('analysis','analysis:42'))

    def test_notification_failure_does_not_mark_sent(self):
        payload = {'type':'notify','message_id':42,'text':'x'}
        # Isolate receipt persistence, not _effect's attempt/result ordering or policy recheck.
        with self.native_source(), patch('secretary.connectors.telegram.notify', return_value=False) as send, \
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
        with patch('secretary.connectors.telegram.notify') as send:
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
        with self.native_source(), patch('secretary.connectors.telegram.notify', return_value=True) as send, \
             patch.object(pipeline.policy, 'prepare_effect', return_value=True), \
             patch.object(pipeline.policy, 'record_receipt', return_value=True):
            pipeline.action(payload)
        send.assert_called_once_with('x', html=False)
        self.assertFalse(any('UPDATE message_analysis' in call.args[0]
                             for call in self.conn.execute.call_args_list))

    def test_email_send_is_blocked_when_the_owner_already_sent_the_draft(self):
        """La bozza remota non è più una bozza: inviare di nuovo duplicherebbe."""
        from secretary.agent import outbox
        draft = dict(account='me@example.test', recipient='luca@example.test', subject='Ciao',
                     body='Testo', approved_by_message=42)
        with patch.object(outbox, 'load_approved', return_value=draft), \
                patch.object(outbox, 'sends_allowed', return_value=True), \
                patch.object(outbox, 'account_for', return_value=dict(address='me@example.test')), \
                patch.object(outbox, 'draft_guard', return_value=False), \
                patch.object(outbox, 'mark') as mark, \
                patch('secretary.connectors.email.send_with_attachments') as send:
            with self.assertRaisesRegex(jobs.Permanent, 'non è più nella cartella Bozze'):
                pipeline.action({'type': 'email', 'id': 1})
        send.assert_not_called()
        mark.assert_called_once_with(self.conn, 1, 'failed')

    def test_email_send_is_deferred_when_the_remote_draft_state_is_unknown(self):
        from secretary.agent import outbox
        draft = dict(account='me@example.test', recipient='luca@example.test', subject='Ciao',
                     body='Testo', approved_by_message=42)
        with patch.object(outbox, 'load_approved', return_value=draft), \
                patch.object(outbox, 'sends_allowed', return_value=True), \
                patch.object(outbox, 'account_for', return_value=dict(address='me@example.test')), \
                patch.object(outbox, 'draft_guard', return_value=None), \
                patch.object(outbox, 'mark') as mark, \
                patch('secretary.connectors.email.send_with_attachments') as send:
            with self.assertRaises(jobs.Deferred):
                pipeline.action({'type': 'email', 'id': 1})
        send.assert_not_called()
        mark.assert_not_called()

    def test_email_send_proceeds_when_the_draft_is_still_a_draft(self):
        from secretary.agent import outbox
        draft = dict(account='me@example.test', recipient='luca@example.test', subject='Ciao',
                     body='Testo', approved_by_message=42)
        with patch.object(outbox, 'load_approved', return_value=draft), \
                patch.object(outbox, 'sends_allowed', return_value=True), \
                patch.object(outbox, 'account_for', return_value=dict(address='me@example.test')), \
                patch.object(outbox, 'draft_guard', return_value=True), \
                patch.object(outbox, 'mark') as mark, \
                patch.object(outbox, 'cleanup_remote') as cleanup, \
                patch.object(pipeline.policy, 'prepare_effect', return_value=True), \
                patch.object(pipeline.policy, 'record_receipt', return_value=True), \
                patch.object(pipeline.policy, 'effect_allowed', return_value=True), \
                patch('secretary.connectors.email.send_with_attachments', return_value=True) as send:
            pipeline.action({'type': 'email', 'id': 1})
        send.assert_called_once()
        mark.assert_called_once_with(self.conn, 1, 'sent')
        cleanup.assert_called_once_with(1)

    def test_discarded_draft_cleanup_removes_the_remote_copy_with_a_receipt(self):
        from secretary.agent import outbox
        self.conn.execute.return_value.fetchone.return_value = ('me@example.test', 'Drafts', '12')
        with patch.object(outbox, 'cleanup_remote', return_value=True) as cleanup, \
                patch.object(pipeline.policy, 'prepare_effect', return_value=True), \
                patch.object(pipeline.policy, 'record_receipt', return_value=True), \
                patch.object(pipeline.policy, 'effect_allowed', return_value=True):
            pipeline.action({'type': 'email_draft_cleanup', 'id': 3, 'evidence_message_id': 42})
        cleanup.assert_called_once_with(3)

    def test_cleanup_without_a_remote_copy_does_nothing(self):
        from secretary.agent import outbox
        self.conn.execute.return_value.fetchone.return_value = ('me@example.test', None, None)
        with patch.object(outbox, 'cleanup_remote') as cleanup, \
                patch.object(pipeline.policy, 'prepare_effect') as prepare:
            pipeline.action({'type': 'email_draft_cleanup', 'id': 3, 'evidence_message_id': 42})
        cleanup.assert_not_called()
        prepare.assert_not_called()

    def test_reminder_without_native_origin_fails_permanently(self):
        # A task without any native origin can never pass the notify gate.
        # reminder_is_current passes, the task row exists (deliverable in
        # principle), but the origin lookup returns nothing: permanent failure.
        with patch('secretary.workflows.task_reconciliation.reminder_is_current', return_value=True):
            self.conn.execute.return_value.fetchone.side_effect = [
                ('Titolo attività',), None]  # task row, then origin lookup
            with patch('secretary.connectors.telegram.notify') as send:
                with self.assertRaisesRegex(jobs.Permanent, 'provenienza nativa'):
                    pipeline.action({'type': 'reminder', 'id': 7, 'deadline': '2099-01-01T00:00:00+00:00'})
            send.assert_not_called()

    def test_stale_reminder_is_not_delivered(self):
        with patch('secretary.workflows.task_reconciliation.reminder_is_current', return_value=False) as gate, \
             patch('secretary.connectors.telegram.notify') as notify:
            pipeline.action({'type': 'reminder', 'id': 7})
        gate.assert_called_once_with(self.conn, {'type': 'reminder', 'id': 7})
        notify.assert_not_called()

    def test_scheduler_stamps_current_deadline(self):
        from datetime import datetime, timezone
        deadline = datetime(2099, 1, 1, tzinfo=timezone.utc)
        # The first SELECT is for owner reminders, the second for legacy deadline reminders.
        self.conn.execute.return_value.fetchall.side_effect = [[], [(7, deadline)]]
        with patch('secretary.core.flows.enabled', return_value=False), patch('secretary.core.jobs.enqueue') as enqueue, \
             patch('secretary.agent.schema.prune'):
            pipeline.schedule()
        self.assertEqual(enqueue.call_args.args[3], {
            'type': 'reminder', 'id': 7, 'deadline': deadline.isoformat()})

    def test_effect_audit_retention_prunes_once_per_day(self):
        # Retention is guarded by a source_cursors day stamp: one DELETE per
        # day, disabled at EFFECT_AUDIT_RETENTION_DAYS=0. Receipts untouched.
        # First schedule(): no stamp -> prune + stamp today. Second: the
        # stamped day matches the real current day -> skip.
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        self.conn.execute.return_value.fetchone.side_effect = [None, ({'day': today},)]
        with patch('secretary.core.flows.enabled', return_value=False), patch('secretary.agent.schema.prune') as agent_prune:
            with patch.object(pipeline.config, 'EFFECT_AUDIT_RETENTION_DAYS', 30):
                pipeline.schedule()
                pipeline.schedule()
        prunes = [call for call in self.conn.execute.call_args_list
                  if 'DELETE FROM effect_audit' in call.args[0]]
        self.assertEqual(len(prunes), 1)
        agent_prune.assert_called_once_with(self.conn)  # stesso passaggio giornaliero
        self.assertIn('interval', prunes[0].args[0])
        self.assertEqual(prunes[0].args[1], (30,))
        stamps = [call for call in self.conn.execute.call_args_list
                  if 'effect_audit_retention_day' in call.args[0]]
        self.assertEqual(len(stamps), 3)  # read + upsert, then read again
        self.conn.execute.reset_mock()
        self.conn.execute.return_value.fetchone.side_effect = [None]
        with patch('secretary.core.flows.enabled', return_value=False), patch('secretary.agent.schema.prune'), \
             patch.object(pipeline.config, 'EFFECT_AUDIT_RETENTION_DAYS', 0):
            pipeline.schedule()
        self.assertFalse(any('DELETE FROM effect_audit' in call.args[0]
                             for call in self.conn.execute.call_args_list))

    def test_notification_ack_marks_actual_sent(self):
        with self.native_source(), patch('secretary.connectors.telegram.notify', return_value=True) as send, \
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

    def test_inbox_message_without_explicit_data_grant_is_deferred_before_ai(self):
        from secretary.agent import turns
        self.conn.execute.return_value.fetchone.return_value = None
        with self.native_source():
            with self.assertRaisesRegex(jobs.Deferred, 'AI text data policy denied'):
                turns._eligible(self.conn, 42, [], primary=True)
            # Un messaggio in attesa (non quello del job) viene solo saltato.
            self.assertFalse(turns._eligible(self.conn, 42, [], primary=False))
        audit = [call.args[1] for call in self.conn.execute.call_args_list
                 if 'INSERT INTO effect_audit' in call.args[0]]
        self.assertIn((42, 'ai_text', False, 'data_denied'), audit)

    def test_inbox_media_requires_the_separate_media_grant(self):
        from secretary.agent import turns
        allowed = lambda conn, mid, effect, **kw: effect in ('extract', 'ai_text')
        with patch.object(pipeline.policy, 'can_operate', side_effect=allowed):
            self.conn.execute.return_value.fetchone.return_value = None
            with self.assertRaisesRegex(jobs.Deferred, 'AI media data policy denied'):
                turns._eligible(self.conn, 42, [dict(id=1)], primary=True)
            self.assertTrue(turns._eligible(self.conn, 42, [], primary=True))

    def test_closed_month_catchup_never_schedules_current_month(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        current = datetime.now(ZoneInfo(pipeline.config.TIMEZONE)).strftime('%Y-%m')
        self.conn.execute.return_value.fetchall.return_value=[]
        with patch('secretary.core.flows.enabled',return_value=True), patch('secretary.core.db.months_with_invoices',return_value=[
            {'entity':'a','month':'2000-01'}, {'entity':'b','month':'2000-02'},
            {'entity':'a','month':current}]), patch('secretary.core.jobs.submit') as submit, \
             patch.object(pipeline.capabilities, 'enabled', return_value=True), \
             patch('secretary.agent.schema.prune'):
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