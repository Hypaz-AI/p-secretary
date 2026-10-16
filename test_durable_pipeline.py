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

    def test_worker_records_error_details(self):
        worker = jobs.Worker('analysis', MagicMock(side_effect=ValueError('example detail')))
        job = (42, {'message_id': 7}, 1, 'token')
        with patch.object(jobs, 'claim', return_value=job), \
             patch.object(jobs, 'finish', side_effect=lambda *args: worker.stop()), \
             patch.object(jobs.logger, 'exception') as log:
            worker.run()
            jobs.finish.assert_called_once_with(job, 'ValueError: example detail')
            log.assert_called_once()

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

    def test_ingest_commits_source_and_queue_without_ai(self):
        with patch('message_store.store', return_value=42) as store, patch('brain.analyze') as ai:
            result = pipeline.ingest('email','sender','subject','body',source_id='id')
        self.assertEqual(result['message_id'],42)
        store.assert_called_once()
        ai.assert_not_called()
        sql, params = self.conn.execute.call_args_list[0].args
        self.assertIn('ON CONFLICT',sql)
        self.assertEqual(params[:2],('analysis','analysis:42'))
        _, reconciliation = self.conn.execute.call_args_list[1].args
        self.assertEqual(reconciliation[0], 'reconcile')
        self.assertEqual(json.loads(reconciliation[2]), {'message_id': 42, 'revision': ''})

    def test_notification_failure_does_not_mark_sent(self):
        with patch('connectors.telegram.notify', return_value=False):
            with self.assertRaises(RuntimeError):
                pipeline.action({'type':'notify','message_id':42,'text':'x'})
        self.conn.execute.assert_not_called()

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
        self.conn.execute.return_value.fetchall.return_value = [(7, deadline)]
        with patch('flows.enabled', return_value=False), patch('jobs.enqueue') as enqueue:
            pipeline.schedule()
        self.assertEqual(enqueue.call_args.args[3], {
            'type': 'reminder', 'id': 7, 'deadline': deadline.isoformat()})

    def test_notification_ack_marks_actual_sent(self):
        with patch('connectors.telegram.notify', return_value=True):
            pipeline.action({'type':'notify','message_id':42,'text':'x'})
        self.assertIn('notified=TRUE', self.conn.execute.call_args.args[0])

    def test_retry_backoff_and_token_fencing(self):
        jobs.finish((1,{},3,'lease-token'),'TimeoutError')
        sql, args = self.conn.execute.call_args.args
        self.assertIn('token=%s', sql)
        self.assertEqual(args,('pending','TimeoutError',False,60,1,'lease-token'))

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
        self.conn.execute.return_value.fetchone.return_value=None
        with patch.object(pipeline,'load_message',return_value=(('email','x','s','body','id'),[])), \
             patch('flows.enabled',return_value=True), \
             patch('brain.analyze',return_value={'category':'errore'}):
            with self.assertRaises(RuntimeError):
                pipeline.analyze({'message_id':42})
        self.assertFalse(any('INSERT INTO message_analysis' in c.args[0] for c in self.conn.execute.call_args_list))

    def test_closed_month_catchup_never_schedules_current_month(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        current = datetime.now(ZoneInfo(pipeline.config.TIMEZONE)).strftime('%Y-%m')
        self.conn.execute.return_value.fetchall.return_value=[]
        with patch('flows.enabled',return_value=True), patch('db.months_with_invoices',return_value=[
            {'entity':'a','month':'2000-01'}, {'entity':'b','month':'2000-02'},
            {'entity':'a','month':current}]), patch('jobs.submit') as submit:
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