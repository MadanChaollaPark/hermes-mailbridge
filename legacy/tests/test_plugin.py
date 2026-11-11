"""Offline behavioral tests. No customer accounts or Gmail writes."""
import fcntl
import json
import os
from pathlib import Path
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from bridge_plugin.service import Service, catalog, exclusive
from hermes_tools import SPECS, invoke
from mailbridge.engine import digest
ROOT = Path(__file__).resolve().parents[1]


def wait(service, tid):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = service.status(tid)
        if not result['worker_active']:
            return result
        time.sleep(.05)
    raise AssertionError('Worker did not settle within 30 seconds')


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.s = Service(self.root / 'runtime')

    def prepare(self, scenario='recovery'):
        tid = self.s.plan(scenario=scenario)['job_id']
        result = wait(self.s, tid)
        self.assertEqual(result['status'], 'complete', result)
        return tid

    def test_catalog_scope_and_honest_support(self):
        entries = catalog()
        additions = [p for p in entries if p['group'] == 'additional_20']
        self.assertEqual(len(entries), 38)
        self.assertEqual(len({p['id'] for p in entries}), 38)
        self.assertEqual(len(additions), 20)
        self.assertGreaterEqual(len({p['country_code'] for p in additions}), 6)
        self.assertTrue(all(p['sources'] and p['integration_status'] == 'not_live_tested' for p in entries))
        self.assertEqual(catalog('bindup')['capture_policy'], 'export_only')
        self.assertNotIn('juno', {p['id'] for p in catalog(pop_only=True)})
        self.assertTrue(all(p['country_code'] == 'GB' for p in catalog(country='gb')))

    def test_detached_recovery_and_readback(self):
        tid = self.prepare()
        self.s.start(tid)
        report = wait(self.s, tid)
        self.assertEqual(report['migration']['covered'], 14, report)
        self.assertEqual(report['migration']['unique_messages'], 12)
        self.assertTrue(report['migration']['destination_complete'])
        self.assertTrue(all(r['attempts'] <= 1 for r in self.s.store.messages(tid)))
        self.s.dispatch('verify', {'tid': tid})
        self.assertTrue(wait(self.s, tid)['migration']['destination_complete'])
        encoded = json.dumps(self.s.report(tid))
        self.assertNotIn('Fictional project note', encoded)
        self.assertNotIn('Synthetic migration fixture', encoded)
        self.assertTrue(self.s.file('reports', tid).exists())

    def test_rejection_is_not_full_destination_coverage(self):
        tid = self.prepare('rejection')
        self.s.start(tid)
        report = wait(self.s, tid)
        self.assertFalse(report['migration']['destination_complete'])
        self.assertLess(report['migration']['covered'], report['migration']['total'])

    def test_corrupted_destination_revokes_complete_status(self):
        tid = self.prepare('clean')
        self.s.start(tid)
        self.assertTrue(wait(self.s, tid)['migration']['destination_complete'])
        row = next(r for r in self.s.store.messages(tid) if not r['canonical'])
        self.s.engine.destination_path(tid, row).write_bytes(b'corrupt destination')
        self.s.dispatch('verify', {'tid': tid})
        report = wait(self.s, tid)
        self.assertFalse(report['migration']['destination_complete'])
        self.assertEqual(report['migration']['status'], 'needs_review')

    def test_source_byte_mutation_blocks_start(self):
        tid = self.prepare()
        source = Path(self.s.store.task(tid)['config']['source_root'])
        file = next(source.rglob('*.eml'))
        file.write_bytes(file.read_bytes() + b'changed\r\n')
        self.s.start(tid)
        result = wait(self.s, tid)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['migration']['covered'], 0)

    def test_task_lock_prevents_second_worker(self):
        tid = self.prepare()
        with exclusive(self.s.root / 'locks' / (tid + '.worker')):
            with self.assertRaises(ValueError):
                self.s.start(tid)

    def test_untrusted_arguments_and_path_traversal(self):
        self.assertFalse(json.loads(invoke('status', {'tid': '../elsewhere'}, self.s.root))['ok'])
        self.assertFalse(json.loads(invoke('start', {'tid': 'abc', 'approved': True}, self.s.root))['ok'])
        self.assertNotIn('approve', SPECS)
        with self.assertRaises(ValueError):
            self.s.file('jobs', '/tmp/evil')

    def test_source_config_does_not_persist_secret(self):
        result = self.s.add_source('cafe24', 'test', 'mail.example.test', 'person@example.test', 'MAILBRIDGE_SECRET_TEST')
        self.assertFalse(result['credential_stored'])
        with mock.patch.dict(os.environ, {'MAILBRIDGE_SECRET_TEST': 'unusual-test-secret'}):
            self.assertTrue(self.s.inspect('test')['credentials_available'])
        self.assertNotIn('unusual-test-secret', self.s.file('connections', 'test').read_text())
        with self.assertRaises(ValueError):
            self.s.add_source('bindup', 'unsafe', 'mail.example.test', 'user', 'MAILBRIDGE_SECRET_TEST')
        with self.assertRaises(ValueError):
            self.s.add_source('cafe24', 'unsafe', 'mail.example.test', 'user', 'PATH')
        with self.assertRaises(ValueError):
            self.s.add_source('cafe24', 'unsafe', 'mail.example.test', 'user', 'MAILBRIDGE_SECRET_TEST', security='plain')

    def test_approval_requires_terminal_and_exact_binding(self):
        tid = self.prepare()
        outcome = subprocess.run([sys.executable, str(ROOT / 'mailbridge_plugin.py'), '--state', str(self.s.root), 'approve', tid], input='', text=True, capture_output=True)
        self.assertNotEqual(outcome.returncode, 0)
        self.assertIn('interactive local terminal', outcome.stdout)
        self.assertFalse(self.s.file('grants', tid).exists())
        config = self.s.store.task(tid)['config']
        fingerprint = self.s.plan_digest(tid)
        with self.assertRaises(ValueError):
            self.s.approve(tid, 'wrong destination', fingerprint)
        self.s.approve(tid, config['destination'], fingerprint)
        with self.s.store.connection() as db:
            db.execute("UPDATE tasks SET kind='gmail' WHERE id=?", (tid,))
        with mock.patch.object(self.s, 'plan_digest', return_value=fingerprint):
            self.assertTrue(self.s.authorized(tid))
        with mock.patch.object(self.s, 'plan_digest', return_value='changed'):
            with self.assertRaises(ValueError):
                self.s.authorized(tid)
        self.s.file('grants', tid).unlink()
        with self.assertRaisesRegex(ValueError, 'local approval'):
            self.s.authorized(tid)

    def test_failed_verification_clears_cached_complete(self):
        tid = self.prepare()
        config = self.s.store.task(tid)['config']
        config['gmail_report'] = dict(accounted_complete=True, gmail_complete=True,
            folder_label_complete=True, final_reconciliation_verified=True)
        with self.s.store.connection() as db:
            db.execute("UPDATE tasks SET kind='gmail' WHERE id=?", (tid,))
        self.s.store.update_task(tid, config=config, status='complete', final_verified=1)
        self.s.save('jobs', tid, dict(id=tid, operation='verify', status='queued', started_at='test'))
        fd = os.open(self.s.root / 'locks' / (tid + '.worker'), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.object(self.s, 'gmail_readback', side_effect=RuntimeError('secret provider text')):
            self.s.worker(tid, fd)
        result = self.s.status(tid)
        self.assertFalse(result['migration']['destination_complete'])
        self.assertFalse(result['migration']['accounted_complete'])
        self.assertEqual(result['status'], 'needs_review')
        self.assertNotIn('secret provider text', json.dumps(result))

    def test_interrupted_job_detection(self):
        tid = self.prepare()
        job = self.s.load('jobs', tid)
        job['status'] = 'running'
        self.s.save('jobs', tid, job)
        self.assertEqual(self.s.status(tid)['status'], 'interrupted')
        self.s.start(tid)
        self.assertTrue(wait(self.s, tid)['migration']['destination_complete'])

    def test_dashboard_cannot_take_over_plugin_runtime(self):
        from mailbridge.server import serve
        with self.assertRaisesRegex(SystemExit, 'Hermes plugin'):
            serve(self.s.root, port=0)

    def test_installer_preserves_state_and_refuses_overwrite(self):
        from install import install
        home = self.root / 'hermes'
        state = home / 'plugin-data/mailbridge/evidence.txt'
        state.parent.mkdir(parents=True)
        state.write_text('keep me')
        target = install(ROOT, home)
        self.assertTrue((target / 'plugin.yaml').is_file())
        self.assertFalse((target / '__pycache__').exists())
        self.assertEqual(state.read_text(), 'keep me')
        with self.assertRaises(ValueError):
            install(ROOT, home)

    def test_gmail_prepare_needs_no_network_and_refuses_unapproved_start(self):
        demo = self.prepare('clean')
        source = self.s.store.task(demo)['config']['source_root']
        oauth = self.root / 'oauth.json'
        oauth.write_text(json.dumps({'installed': {'client_id': 'fictional.apps.googleusercontent.com',
            'client_secret': 'not-a-real-secret', 'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token'}}))
        profile = json.loads((ROOT / 'examples/profile.example.json').read_text())
        profile.update(source_root=source, source_mailbox='person@example.test', destination='person@example.test',
            state_dir=str(self.root / 'gmail-state'), oauth_client=str(oauth),
            token_store={'type': 'file', 'path': str(self.root / 'token.json')})
        path = self.root / 'profile.json'
        path.write_text(json.dumps(profile))
        tid = self.s.plan(mode='gmail', profile=str(path))['job_id']
        result = wait(self.s, tid)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(result['migration']['status'], 'awaiting_approval')
        self.assertEqual(result['migration']['total'], 14)
        self.assertFalse((self.root / 'token.json').exists())
        with self.assertRaisesRegex(ValueError, 'local approval'):
            self.s.start(tid)
        profile['label_prefix'] = 'Changed after approval'
        path.write_text(json.dumps(profile))
        with self.assertRaisesRegex(ValueError, 'profile changed'):
            self.s.plan_digest(tid)

    def test_loopback_tls_pop_capture_resume_then_rehearsal(self):
        cert, key = self.root / 'cert.pem', self.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=DNS:localhost'], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        commands = []
        messages = [f'Message-ID: <tls-{i}@example.test>\r\nDate: Mon, 02 Nov 2026 09:00:00 +0900\r\nFrom: a@example.test\r\nTo: b@example.test\r\nSubject: TLS fixture {i}\r\n\r\nBody {i}\r\n'.encode() for i in range(3)]

        class Handler(socketserver.StreamRequestHandler):
            def setup(self):
                self.request = context.wrap_socket(self.request, server_side=True)
                super().setup()

            def finish(self):
                try:
                    super().finish()
                finally:
                    self.request.close()

            def handle(self):
                self.wfile.write(b'+OK test POP\r\n')
                while line := self.rfile.readline():
                    command, *arg = line.decode().strip().split()
                    commands.append(command)
                    if command in {'USER', 'PASS'}:
                        self.wfile.write(b'+OK\r\n')
                    elif command == 'UIDL':
                        self.wfile.write(b'+OK\r\n1 uid-one\r\n2 uid-two\r\n3 uid-three\r\n.\r\n')
                    elif command == 'RETR':
                        self.wfile.write(b'+OK\r\n' + messages[int(arg[0])-1] + b'.\r\n')
                    elif command == 'QUIT':
                        self.wfile.write(b'+OK bye\r\n')
                        return
                    else:
                        self.wfile.write(b'-ERR\r\n')

        with socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.s.add_source('cafe24', 'loopback', 'localhost', 'fixture', 'MAILBRIDGE_SECRET_FIXTURE', port=server.server_address[1])
                with mock.patch.dict(os.environ, {'SSL_CERT_FILE': str(cert), 'MAILBRIDGE_SECRET_FIXTURE': 'test-secret-only'}):
                    for expected in (2, 1, 0):
                        job = self.s.capture('loopback', limit=2)['job_id']
                        result = wait(self.s, job)
                        self.assertEqual(result['status'], 'complete', result)
                        self.assertEqual(result['result']['downloaded_this_run'], expected)
                        self.assertEqual(result['result']['source_deleted'], 0)
                tid = self.s.plan(mode='local', connection_id='loopback')['job_id']
                self.assertEqual(wait(self.s, tid)['status'], 'complete')
                self.s.start(tid)
                self.assertEqual(wait(self.s, tid)['migration']['covered'], 3)
            finally:
                server.shutdown()
                thread.join()
        self.assertNotIn('DELE', commands)
        self.assertEqual(commands.count('RETR'), 3)
        self.assertTrue(all('test-secret-only' not in p.read_text() for p in self.s.root.rglob('*.json')))
        captured = list((self.s.root / 'captures/loopback/Inbox').glob('*.eml'))
        self.assertEqual({digest(p.read_bytes()) for p in captured}, {digest(m) for m in messages})


if __name__ == '__main__':
    unittest.main()
