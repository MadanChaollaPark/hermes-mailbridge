"""Connection behavior with fake Google responses; never accesses a real account."""
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import gmail_eml_migration_v2 as core
from bridge_plugin.gmail_connection import GmailConnections
from bridge_plugin.service import Service, exclusive
from hermes_tools import invoke


class GmailConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.service = Service(self.root / 'runtime')
        self.gmail = GmailConnections(self.service)
        self.client = self.root / 'client.json'
        self.client.write_text(json.dumps({'installed': {'client_id': 'fictional.apps.googleusercontent.com',
            'client_secret': 'fictional-secret', 'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token'}}))
        self.record = dict(connection_id='gmail-fixture', status='starting', requested_email=None,
            oauth_client=str(self.client), client_id=core.read_oauth_client_config(self.client)[0],
            client_hash=core.read_oauth_client_config(self.client)[1], created_at='test')
        self.credentials = mock.Mock(refresh_token='fictional-refresh', scopes=core.REQUIRED_SCOPES)
        self.credentials.to_json.return_value = json.dumps(dict(refresh_token='fictional-refresh',
            token='fictional-access', client_id=self.record['client_id'], scopes=core.REQUIRED_SCOPES))
        self.flow = mock.Mock(credentials=self.credentials)
        self.flow.oauth2session.token = {'scope': ' '.join(core.REQUIRED_SCOPES)}
        self.api = mock.Mock()
        self.api.users.return_value.getProfile.return_value.execute.return_value = {'emailAddress': 'chosen@example.test'}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(core, 'KEYCHAIN_SECURITY', self.root / 'missing-security-command').start()
        mock.patch.object(core, 'build_gmail_service', return_value=self.api).start()

    def test_pending_signin_always_returns_clickable_link_even_if_launch_fails(self):
        self.record.update(status='awaiting_browser', authorization_url='https://accounts.google.com/o/oauth2/auth?state=fixture', browser_launch_accepted=False)
        self.gmail.save(self.record)
        with exclusive(self.service.root / 'locks/gmail-fixture.gmail-auth'):
            result = self.gmail.connect(oauth_client=str(self.client))
        self.assertIn('[Choose your Gmail account](https://accounts.google.com/', result['sign_in_link'])
        self.assertIn('Always show', result['next_action'])
        self.assertFalse(result['browser_launch_accepted'])

    def test_macos_launcher_uses_chrome_and_reports_rejection(self):
        with mock.patch('bridge_plugin.gmail_connection.sys.platform', 'darwin'), mock.patch('bridge_plugin.gmail_connection.subprocess.Popen') as launch:
            launch.return_value.wait.return_value = 1
            self.assertFalse(self.gmail.launch_browser('https://accounts.google.com/'))
            self.assertEqual(launch.call_args.args[0], ['/usr/bin/open','-a','Google Chrome','https://accounts.google.com/'])
            self.assertEqual(launch.return_value.wait.call_args.kwargs['timeout'],3)

    def test_profile_target_is_one_argument_without_shell_interpolation(self):
        with mock.patch('bridge_plugin.gmail_connection.sys.platform', 'darwin'), mock.patch.object(self.gmail,'settings',return_value={'chrome_profile_directory':'Profile 9'}), mock.patch('bridge_plugin.gmail_connection.subprocess.Popen') as launch:
            launch.return_value.wait.return_value = 0
            self.assertTrue(self.gmail.launch_browser('https://accounts.google.com/'))
            self.assertIn('--profile-directory=Profile 9', launch.call_args.args[0])
            self.assertNotIn('shell',launch.call_args.kwargs)

    def test_no_client_explains_local_setup_instead_of_hosted_connector(self):
        result = self.gmail.connect()
        self.assertEqual(result['status'], 'setup_required')
        self.assertIn('Desktop', result['next_action'])
        self.assertNotIn('credentials', result)

    def test_connect_spawns_detached_worker_and_reuses_in_progress_flow(self):
        with mock.patch('bridge_plugin.gmail_connection.subprocess.Popen') as spawn:
            result = self.gmail.connect(oauth_client=str(self.client))
            cid = result['connection_id']
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])
            self.assertEqual(spawn.call_args.kwargs['stdout'], -3)
            with exclusive(self.service.root / 'locks' / (cid + '.gmail-auth')):
                again = self.gmail.connect(oauth_client=str(self.client))
                self.assertEqual(again['connection_id'], cid)
                self.assertEqual(spawn.call_count, 1)
                different = self.gmail.connect(email='other@example.test', oauth_client=str(self.client))
                self.assertEqual(different['status'], 'sign_in_in_progress')

    def test_chooser_identity_is_verified_saved_and_redacted(self):
        self.gmail.finish(self.record, self.flow, {})
        result = self.gmail.status('gmail-fixture')
        self.assertEqual(result['status'], 'connected')
        self.assertEqual(result['email'], 'chosen@example.test')
        self.assertEqual(result['gmail_writes'], 0)
        self.api.users.return_value.getProfile.assert_called_once_with(userId='me')
        profile = core.load_profile(Path(self.gmail.record('gmail-fixture')['profile']))
        token = json.loads(core.load_token_json(profile))
        self.assertEqual(token['_migration_binding']['destination'], 'chosen@example.test')
        self.assertEqual(profile.token_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('fictional-refresh', json.dumps(result))
        self.assertNotIn('fictional-access', json.dumps(result))
        self.assertNotIn('profile', result)

    def test_wrong_account_does_not_save_credentials(self):
        self.record['requested_email'] = 'intended@example.test'
        with mock.patch.object(core, 'save_token_json') as save:
            with self.assertRaisesRegex(ValueError, 'different account'):
                self.gmail.finish(self.record, self.flow, {})
            save.assert_not_called()
        self.assertFalse((self.service.root / 'gmail-accounts').exists())

    def test_missing_scopes_or_refresh_token_never_claims_connected(self):
        for missing in ['scope', 'refresh']:
            with self.subTest(missing=missing), mock.patch.object(core, 'save_token_json') as save:
                self.flow.oauth2session.token = {'scope': core.REQUIRED_SCOPES[0]} if missing == 'scope' else {'scope': ' '.join(core.REQUIRED_SCOPES)}
                self.credentials.refresh_token = 'fictional-refresh' if missing == 'scope' else None
                with self.assertRaises(ValueError):
                    self.gmail.finish(self.record, self.flow, {})
                save.assert_not_called()

    def test_changed_client_during_signin_is_rejected(self):
        self.client.write_text(self.client.read_text() + '\n')
        with mock.patch.object(core, 'save_token_json') as save:
            with self.assertRaisesRegex(ValueError, 'client changed'):
                self.gmail.finish(self.record, self.flow, {})
            save.assert_not_called()

    def test_interrupted_worker_does_not_report_connected_or_stale_url(self):
        self.record.update(status='awaiting_browser', authorization_url='https://accounts.google.com/fake')
        self.gmail.save(self.record)
        status = self.gmail.status('gmail-fixture')
        self.assertEqual(status['status'], 'interrupted')
        self.assertNotIn('authorization_url', status)
        with exclusive(self.service.root / 'locks' / 'gmail-fixture.gmail-auth'):
            self.assertIn('authorization_url', self.gmail.status('gmail-fixture'))

    def test_provider_exception_does_not_leak_tokens(self):
        self.gmail.save(self.record)
        path = self.service.root / 'locks/gmail-fixture.gmail-auth'
        path.parent.mkdir(exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with mock.patch.object(core, 'google_dependencies', side_effect=RuntimeError('secret-provider-token')):
            self.gmail.worker('gmail-fixture', fd)
        result = self.gmail.status('gmail-fixture')
        self.assertEqual(result['status'], 'not_connected')
        self.assertNotIn('secret-provider-token', json.dumps(result))
        self.assertFalse(result['migration_started'])

    def test_connected_account_builds_profile_without_changing_existing_profile(self):
        self.gmail.finish(self.record, self.flow, {})
        original_path = Path(self.gmail.record('gmail-fixture')['profile'])
        original = original_path.read_bytes()
        source = self.root / 'exports'
        source.mkdir()
        path = self.gmail.plan_profile('gmail-fixture', str(source), 'legacy@example.test', 'Demo Archive')
        profile = core.load_profile(Path(path))
        original_profile = core.load_profile(original_path)
        self.assertEqual(profile.destination, 'chosen@example.test')
        self.assertEqual(profile.source_mailbox, 'legacy@example.test')
        self.assertEqual(profile.token_file, original_profile.token_file)
        self.assertEqual(profile.keychain_account, original_profile.keychain_account)
        self.assertEqual(original_path.read_bytes(), original)
        self.assertFalse(profile.manifest_path.exists())
        self.assertFalse((self.service.root / 'grants').exists())

    def test_connection_plan_keeps_local_approval_gate(self):
        from test_plugin import wait
        self.gmail.finish(self.record, self.flow, {})
        source = self.root / 'exports'
        source.mkdir()
        (source / 'one.eml').write_bytes(b'Message-ID: <test@example.test>\r\nDate: Wed, 11 Nov 2026 10:00:00 +0900\r\nFrom: source@example.test\r\nTo: chosen@example.test\r\nSubject: Fictional fixture\r\n\r\nBody\r\n')
        tid = self.service.plan(mode='gmail', gmail_connection='gmail-fixture', source=str(source), source_mailbox='source@example.test')['job_id']
        result = wait(self.service, tid)
        self.assertEqual(result['migration']['status'], 'awaiting_approval')
        self.assertEqual(result['destination'], 'chosen@example.test')
        with self.assertRaisesRegex(ValueError, 'local approval'):
            self.service.start(tid)

    def test_tool_runtime_preserves_managed_libraries_without_host_auth_secrets(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'host-secret'}), mock.patch('hermes_tools.subprocess.run') as run:
            run.return_value.stdout = '{"ok":true,"connections":[]}'
            result = json.loads(invoke('gmail_connection_status', {}, self.service.root))
            self.assertTrue(result['ok'])
            self.assertIn(str(Path(__file__).resolve().parents[1]), run.call_args.kwargs['env']['PYTHONPATH'])
            self.assertNotIn('OPENAI_API_KEY', run.call_args.kwargs['env'])


if __name__ == '__main__':
    unittest.main()
