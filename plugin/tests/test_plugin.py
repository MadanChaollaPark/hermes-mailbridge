"""Offline behavioral tests. No customer accounts or Gmail writes."""
import fcntl
import json
import os
import poplib
from pathlib import Path
import socketserver
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import gmail_eml_migration_v2 as core
from bridge_plugin.gmail_transport import GmailHttpError
from bridge_plugin.service import Service, catalog, exclusive
from hermes_tools import SPECS, invoke
from mailbridge.engine import digest
from mailbridge.pop3_source import capture
from mailbridge_plugin import public_error
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

    def test_public_error(self):
        class DerivedMigrationError(core.MigrationError):
            pass

        self.assertEqual(public_error(ValueError('x')), 'x')
        message = 'Profile schema_version must be 1.'
        self.assertEqual(public_error(core.MigrationError(message)), message)
        self.assertEqual(public_error(KeyError('Migration not found')), 'Migration not found')
        generic = 'Inspect local configuration or job evidence; no automatic retry was scheduled.'
        for exc in (
            KeyError('other'),
            RuntimeError('secret body'),
            GmailHttpError(500, b'{"token":"secret body"}'),
            json.JSONDecodeError('m', 'doc', 0),
            DerivedMigrationError('secret body'),
        ):
            with self.subTest(error_type=type(exc).__name__):
                self.assertEqual(public_error(exc), generic)

    def test_call_reports_missing_migration(self):
        result = subprocess.run([
            sys.executable, str(ROOT / 'mailbridge_plugin.py'),
            '--state', str(self.s.root), 'call',
        ], input=json.dumps({'action': 'verify', 'arguments': {'tid': 'nosuchtask1'}}),
            text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {
            'ok': False, 'error_type': 'KeyError', 'error': 'Migration not found',
        })

    def test_call_reports_bad_gmail_profile(self):
        profile = self.root / 'profile.json'
        profile.touch(mode=0o600)
        profile.write_text(json.dumps({'schema_version': 999}))
        result = subprocess.run([
            sys.executable, str(ROOT / 'mailbridge_plugin.py'),
            '--state', str(self.s.root), 'call',
        ], input=json.dumps({'action': 'plan', 'arguments': {
            'mode': 'gmail', 'profile': str(profile),
        }}), text=True, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout), {
            'ok': False, 'error_type': 'MigrationError',
            'error': 'Profile schema_version must be 1.',
        })

    def test_source_config_does_not_persist_secret(self):
        result = self.s.add_source('cafe24', 'test', 'mail.example.test', 'person@example.test', 'MAILBRIDGE_POP_PASSWORD')
        self.assertFalse(result['credential_stored'])
        with mock.patch.dict(os.environ, {'MAILBRIDGE_POP_PASSWORD': 'unusual-test-secret'}):
            self.assertTrue(self.s.inspect('test')['credentials_available'])
        self.assertNotIn('unusual-test-secret', self.s.file('connections', 'test').read_text())
        with self.assertRaises(ValueError):
            self.s.add_source('bindup', 'unsafe', 'mail.example.test', 'user', 'MAILBRIDGE_POP_PASSWORD')
        with self.assertRaises(ValueError):
            self.s.add_source('cafe24', 'unsafe', 'mail.example.test', 'user', 'PATH')
        with self.assertRaises(ValueError):
            self.s.add_source('cafe24', 'unsafe', 'mail.example.test', 'user', 'MAILBRIDGE_POP_PASSWORD', security='plain')

    def test_source_port_defaults_follow_security(self):
        self.s.add_source('cafe24', 'tlsdef', 'mail.example.test', 'u', 'MAILBRIDGE_POP_PASSWORD')
        tls = json.loads(self.s.file('connections', 'tlsdef').read_text())
        self.assertEqual(tls['port'], 995)
        self.assertEqual(tls['security'], 'tls')

        self.s.add_source('cafe24', 'stlsdef', 'mail.example.test', 'u',
                          'MAILBRIDGE_POP_PASSWORD', security='starttls')
        starttls = json.loads(self.s.file('connections', 'stlsdef').read_text())
        self.assertEqual(starttls['port'], 110)

        self.s.add_source('cafe24', 'stlsexp', 'mail.example.test', 'u',
                          'MAILBRIDGE_POP_PASSWORD', security='starttls', port=2110)
        explicit = json.loads(self.s.file('connections', 'stlsexp').read_text())
        self.assertEqual(explicit['port'], 2110)

        for port in (0, '995'):
            with self.subTest(port=port), self.assertRaises(ValueError):
                self.s.add_source('cafe24', 'invalid', 'mail.example.test', 'u',
                                  'MAILBRIDGE_POP_PASSWORD', port=port)

    def test_source_add_cli_starttls_defaults_to_110(self):
        result = subprocess.run([
            sys.executable, str(ROOT / 'mailbridge_plugin.py'),
            '--state', str(self.s.root), 'source-add',
            '--provider', 'cafe24', '--connection-id', 'clistls',
            '--host', 'mail.example.test', '--username', 'u', '--security', 'starttls',
        ], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stdout or result.stderr)
        connection = json.loads(self.s.file('connections', 'clistls').read_text())
        self.assertEqual(connection['port'], 110)

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

    def gmail_profile(self):
        demo = self.prepare('clean')
        source = self.s.store.task(demo)['config']['source_root']
        profile = json.loads((ROOT / 'examples/profile.example.json').read_text())
        profile.update(source_root=source, source_mailbox='person@example.test', destination='person@example.test',
            state_dir=str(self.root / 'gmail-state'), auth_provider="google",
            token_store={'type': 'hermes'})
        path = self.root / 'profile.json'
        path.write_text(json.dumps(profile))
        return path, profile

    def test_worker_env_has_google_client_only_for_gmail_tasks(self):
        profile, config = self.gmail_profile()
        google = {key: 'fictional-' + key for key in (
            'MAILBRIDGE_GOOGLE_CLIENT_ID', 'MAILBRIDGE_GOOGLE_CLIENT_SECRET')}
        with mock.patch('bridge_plugin.service.secret', side_effect=google.get), \
             mock.patch('bridge_plugin.service.subprocess.Popen', new_callable=mock.MagicMock) as popen:
            self.s.plan(scenario='recovery')
            popen.assert_called_once()
            self.assertTrue(google.keys().isdisjoint(popen.call_args.kwargs['env']))

            popen.reset_mock()
            self.s.plan(mode='local', source=config['source_root'])
            popen.assert_called_once()
            self.assertTrue(google.keys().isdisjoint(popen.call_args.kwargs['env']))

            popen.reset_mock()
            self.s.plan(mode='gmail', profile=str(profile))
            popen.assert_called_once()
            env = popen.call_args.kwargs['env']
            self.assertEqual({key: env[key] for key in google}, google)

    def test_tool_shim_forwards_google_client_only_for_gmail_plan(self):
        google = {key: 'fictional-' + key for key in (
            'MAILBRIDGE_GOOGLE_CLIENT_ID', 'MAILBRIDGE_GOOGLE_CLIENT_SECRET')}
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout='{"ok": true}', stderr='')
        with mock.patch('agent.secret_scope.get_secret', side_effect=google.get), \
             mock.patch('hermes_tools.subprocess.run', return_value=result) as run:
            for action, args, forwards_google in (
                ('plan', {}, False),
                ('plan', {'mode': 'demo'}, False),
                ('plan', {'mode': 'local', 'source': '/x/eml'}, False),
                ('plan', {'mode': 'gmail', 'profile': '/x/p.json'}, True),
                ('gmail_connect', {}, True),
                ('capture', {'connection_id': 'c1'}, False),
            ):
                with self.subTest(action=action, args=args):
                    run.reset_mock()
                    self.assertTrue(json.loads(invoke(action, args, self.s.root))['ok'])
                    run.assert_called_once()
                    env = run.call_args.kwargs['env']
                    self.assertEqual({key: env[key] for key in google if key in env},
                                     google if forwards_google else {})

    def test_gmail_prepare_needs_no_network_and_refuses_unapproved_start(self):
        path, profile = self.gmail_profile()
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

    def test_gmail_prepare_writes_private_engine_log(self):
        profile, _ = self.gmail_profile()
        tid = self.s.plan(mode='gmail', profile=str(profile))['job_id']
        result = wait(self.s, tid)
        self.assertEqual(result['migration']['status'], 'awaiting_approval')
        logs = self.s.root / 'logs'
        path = logs / f'{tid}.prepare.log'
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn('prepare: PASS', path.read_text())
        self.assertEqual(stat.S_IMODE(logs.stat().st_mode), 0o700)
        self.assertEqual(result['engine_logs'], [str(path)])

    def test_gmail_core_subprocess_has_no_timeout(self):
        profile, _ = self.gmail_profile()
        tid = self.s.plan(mode='gmail', profile=str(profile))['job_id']
        self.assertEqual(wait(self.s, tid)['migration']['status'], 'awaiting_approval')
        path = self.s.root / 'logs' / f'{tid}.run.log'
        path.write_text('previous output\n')
        path.chmod(0o644)

        def stopped_engine(argv, *, stdin, stdout, stderr):
            os.write(stdout, b'engine stdout\n')
            os.write(stderr, b'engine stderr\n')
            return subprocess.CompletedProcess(args=argv, returncode=1)

        with mock.patch('mailbridge.engine.subprocess.run', side_effect=stopped_engine) as run:
            self.s.engine.run_gmail(tid)

        run.assert_called_once()
        kwargs = run.call_args.kwargs
        self.assertNotIn('timeout', kwargs)
        self.assertNotIn('capture_output', kwargs)
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertIsInstance(kwargs['stdout'], int)
        self.assertIsInstance(kwargs['stderr'], int)
        self.assertEqual(kwargs['stdout'], kwargs['stderr'])
        result = self.s.status(tid)
        self.assertEqual(result['migration']['status'], 'needs_review')
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(result['engine_logs'], [str(path.with_name(f'{tid}.prepare.log')), str(path)])
        previous, header, stdout, stderr = path.read_text().splitlines()
        self.assertEqual(previous, 'previous output')
        self.assertRegex(header, r'^== .+\+00:00 run$')
        self.assertEqual(stdout, 'engine stdout')
        self.assertEqual(stderr, 'engine stderr')

    def test_engine_log_refuses_symlinked_log_dir(self):
        target = self.root / 'other'
        target.mkdir()
        (self.s.root / 'logs').symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'State must not be a symlink'):
            self.s.engine._core_log('task', 'prepare')
        self.assertEqual(list(target.iterdir()), [])

    def test_abandon_frees_gmail_destination(self):
        path, _ = self.gmail_profile()
        tid = self.s.plan(mode='gmail', profile=str(path))['job_id']
        self.assertEqual(wait(self.s, tid)['migration']['status'], 'awaiting_approval')
        with self.assertRaisesRegex(ValueError, 'already owns'):
            self.s.plan(mode='gmail', profile=str(path))
        self.s.save('grants', tid, {'scope': 'run_this_frozen_plan'})
        self.s.store.update_task(tid, approved=1, final_verified=1)
        self.assertEqual(self.s.abandon(tid), {
            'job_id': tid, 'abandoned': True, 'previous_status': 'awaiting_approval',
        })
        task = self.s.store.task(tid)
        self.assertEqual(task['status'], 'abandoned')
        self.assertEqual(task['approved'], 0)
        self.assertEqual(task['final_verified'], 0)
        self.assertFalse(self.s.file('grants', tid).exists())
        new_tid = self.s.plan(mode='gmail', profile=str(path))['job_id']
        self.assertNotEqual(new_tid, tid)
        self.assertEqual(wait(self.s, new_tid)['migration']['status'], 'awaiting_approval')

    def test_abandoned_task_cannot_be_relaunched(self):
        tid = self.prepare()
        self.s.abandon(tid)
        with self.assertRaisesRegex(ValueError, 'abandoned'):
            self.s.start(tid)
        for action in ('verify', 'reconcile'):
            with self.assertRaisesRegex(ValueError, 'abandoned'):
                self.s.dispatch(action, {'tid': tid})
        with self.assertRaisesRegex(ValueError, 'already finished or abandoned'):
            self.s.abandon(tid)

    def test_abandon_refuses_active_worker_and_uncertain_rows(self):
        tid = self.prepare()
        with exclusive(self.s.root / 'locks' / (tid + '.worker')):
            with self.assertRaisesRegex(ValueError, 'active worker'):
                self.s.abandon(tid)
        row = self.s.store.messages(tid)[0]
        for status in ('uncertain', 'inflight'):
            self.s.store.message(tid, row['id'], status=status)
            with self.assertRaisesRegex(ValueError, 'Reconcile'):
                self.s.abandon(tid)
        self.s.store.message(tid, row['id'], status=row['status'])
        self.s.store.update_task(tid, status='complete')
        with self.assertRaisesRegex(ValueError, 'already finished or abandoned'):
            self.s.abandon(tid)

    def test_abandon_cli_is_operator_only(self):
        tid = self.prepare()
        result = subprocess.run([
            sys.executable, str(ROOT / 'mailbridge_plugin.py'),
            '--state', str(self.s.root), 'abandon', tid,
        ], capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True)
        output = json.loads(result.stdout)
        self.assertIs(output['ok'], True)
        self.assertIs(output['abandoned'], True)
        self.assertNotIn('abandon', SPECS)
        with self.assertRaisesRegex(ValueError, 'Unknown action'):
            self.s.dispatch('abandon', {'tid': tid})

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
                self.s.add_source('cafe24', 'loopback', 'localhost', 'fixture', 'MAILBRIDGE_POP_PASSWORD', port=server.server_address[1])
                with mock.patch.dict(os.environ, {'SSL_CERT_FILE': str(cert), 'MAILBRIDGE_POP_PASSWORD': 'test-secret-only'}):
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

    def test_pop_capture_accepts_long_lines(self):
        previous_maxline = poplib._MAXLINE
        cert, key = self.root / 'cert.pem', self.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
            '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost',
            '-addext', 'subjectAltName=DNS:localhost'], check=True, capture_output=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        commands = []
        messages = [
            b'Subject: Long HTML\r\nContent-Type: text/html\r\n\r\n'
            + b'<p>' + b'x' * 100000 + b'</p>\r\n',
            b'Subject: Short message\r\n\r\nShort body\r\n',
        ]
        ctx = ssl.create_default_context(cafile=str(cert))

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
                        self.wfile.write(b'+OK\r\n1 uid-long\r\n2 uid-short\r\n.\r\n')
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
                result = capture(
                    'localhost', 'fixture', 'test-secret-only', self.root / 'cap',
                    security='tls', port=server.server_address[1],
                    connect=lambda host, port: poplib.POP3_SSL(host, port, timeout=30, context=ctx),
                )
            finally:
                server.shutdown()
                thread.join()

        self.assertEqual(result['downloaded_this_run'], 2)
        captured = (self.root / 'cap' / 'Inbox').glob('*.eml')
        self.assertCountEqual([digest(p.read_bytes()) for p in captured],
                              [digest(message) for message in messages])
        self.assertEqual(poplib._MAXLINE, previous_maxline)
        self.assertNotIn('DELE', commands)


if __name__ == '__main__':
    unittest.main()
