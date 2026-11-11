"""Browser OAuth onboarding, separate from migration approval and Gmail writes."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import time
from datetime import datetime, timedelta, timezone

import gmail_eml_migration_v2 as core
from bridge_plugin.service import PACKAGE, exclusive, valid_id
from mailbridge.store import now


class ConnectionProblem(ValueError):
    """A fixed, safe message that can be shown to the operator."""


class GmailConnections:
    def __init__(self, service):
        self.service = service
        self.root = service.root

    def record(self, cid):
        return self.service.load('gmail-connections', valid_id(cid))

    def save(self, record):
        self.service.save('gmail-connections', record['connection_id'], record)

    def settings(self):
        path = self.root / 'gmail-settings.json'
        return json.loads(path.read_text()) if path.is_file() else {}

    def active(self, cid):
        try:
            with exclusive(self.root / 'locks' / (valid_id(cid) + '.gmail-auth')):
                return False
        except ValueError:
            return True

    def status(self, connection_id=None):
        if connection_id is None:
            return {'connections': [self.status(p.stem) for p in sorted(
                (self.root / 'gmail-connections').glob('*.json'))],
                'oauth_client_configured': bool(self.settings().get('oauth_client'))}
        record = self.record(connection_id)
        active = self.active(connection_id)
        public = {k: record[k] for k in ('connection_id', 'status', 'requested_email',
            'email', 'created_at', 'verified_at', 'expires_at', 'browser_launch_accepted', 'error', 'notice') if k in record}
        public.update(worker_active=active, migration_started=False, gmail_writes=0)
        if record['status'] in {'starting', 'awaiting_browser', 'verifying'} and not active:
            public['status'] = 'interrupted'
            public['error'] = 'Sign-in stopped before account verification. Start connect Gmail again.'
        if active and record['status'] == 'awaiting_browser':
            public['authorization_url'] = record.get('authorization_url')
            public['sign_in_link'] = '[Choose your Gmail account](' + record['authorization_url'] + ')'
            public['next_action'] = 'Always show sign_in_link to the user, even if browser_launch_accepted is true. That flag only confirms the launch request, not a visible browser. Ask them to click the link if no window appeared, complete Google consent, then check this connection ID.'
        if public['status'] == 'connected':
            public['next_action'] = 'Account verified at the shown time. Choose an EML source or capture source mail, then prepare a plan with gmail_connection. Import still needs separate local approval.'
        return public

    def connect(self, email=None, oauth_client=None):
        if email is not None:
            email = core.normalize_mailbox(email, 'email')
        settings = self.settings()
        client = oauth_client or settings.get('oauth_client')
        if not client:
            return {'status': 'setup_required', 'next_action': 'Supply the absolute path of a Google Desktop OAuth client JSON via oauth_client, or configure gmail-settings.json locally. Never paste client secrets or tokens in chat. This is Mailbridge local OAuth; the hosted connector is not used.'}
        path = Path(client).expanduser()
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError('OAuth client must be an absolute, regular, non-symlink Desktop JSON file')
        client_id, client_hash = core.read_oauth_client_config(path)
        with exclusive(self.root / 'locks' / 'gmail-connect-create'):
            # Repeated calls while the browser is open reuse its callback listener.
            for previous in (self.root / 'gmail-connections').glob('*.json'):
                record = self.record(previous.stem)
                if self.active(previous.stem):
                    if record.get('requested_email') == email and record['client_hash'] == client_hash:
                        return self.status(previous.stem)
                    return {'status': 'sign_in_in_progress', 'connection_id': previous.stem,
                            'next_action': 'Finish the current Google sign-in or wait for its ten-minute timeout before choosing another account.'}
            cid = 'gmail-' + uuid.uuid4().hex[:12]
            record = dict(connection_id=cid, status='starting', requested_email=email,
                          oauth_client=str(path.resolve()), client_id=client_id,
                          client_hash=client_hash, created_at=now())
            if settings.get('testing'):
                record['notice'] = 'This Google OAuth app is in Testing. If Google blocks the selected account, its owner must add that email as a test user in Google Cloud. You may choose any permitted Gmail or Workspace account; the ChatGPT account does not determine the Gmail destination.'
            self.save(record)
            lock = self.root / 'locks' / (cid + '.gmail-auth')
            fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                subprocess.Popen([sys.executable, str(PACKAGE / 'mailbridge_plugin.py'),
                    '--state', str(self.root), 'gmail-auth-worker', cid, str(fd)],
                    pass_fds=(fd,), start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            finally:
                os.close(fd)
        # Return a usable link in this same tool call, not a premature success claim.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.status(cid)
            if result['status'] != 'starting':
                return result
            time.sleep(.05)
        result['next_action'] = 'Sign-in link is still being prepared. Call gmail_connection_status before instructing the user to sign in.'
        return result

    def launch_browser(self, url):
        settings = self.settings()
        if sys.platform == 'darwin':
            # LaunchServices avoids Python webbrowser's AppleScript automation path.
            profile = settings.get('chrome_profile_directory')
            command = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                       '--profile-directory=' + profile, url] if profile else ['/usr/bin/open', '-a', 'Google Chrome', url]
        else:
            command = [sys.executable, '-m', 'webbrowser', '-t', url]
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                return process.wait(timeout=3) == 0
            except subprocess.TimeoutExpired:
                return True  # An app may remain running; this does not prove visibility.
        except OSError:
            return False

    def _profile(self, record, email):
        directory = self.root / 'gmail-accounts' / record['connection_id']
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        source = directory / 'empty-source'
        source.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_store = {'type': 'keychain', 'service': 'org.mailbridge.gmail'} if core.KEYCHAIN_SECURITY.is_file() else {
            'type': 'file', 'path': str(directory / 'token.json')}
        profile = dict(schema_version=1, migration_id=record['connection_id'],
            source_root=str(source), source_mailbox=email, destination=email,
            state_dir=str(directory / 'auth-state'), oauth_client=record['oauth_client'],
            label_prefix='Mailbridge Archive', custom_label_policy='archive_prefix_and_folder',
            root_folder='Inbox', inbox_folders=['Inbox'], progress_every=50,
            google_rejection_policy='stop', missing_message_id_policy='stop',
            message_id_collision_policy='stop_on_distinct_prepared_messages', token_store=token_store)
        path = directory / 'profile.json'
        core.write_json_atomic(path, profile, mode=0o600)
        return core.load_profile(path)

    def finish(self, record, flow, dependencies):
        credentials = flow.credentials
        scopes = core.normalize_scope_values(flow.oauth2session.token.get('scope', ())) or core.normalize_scope_values(
            getattr(credentials, 'granted_scopes', None) or getattr(credentials, 'scopes', None))
        if not credentials.refresh_token or not set(core.REQUIRED_SCOPES).issubset(scopes):
            raise ConnectionProblem('Google did not grant every required permission and offline access. Reconnect and review all migration permissions.')
        service = core.build_gmail_service(credentials, dependencies)
        actual = core.normalize_mailbox(service.users().getProfile(userId='me').execute(num_retries=2).get('emailAddress'), 'Google account')
        if record.get('requested_email') and actual != record['requested_email']:
            raise ConnectionProblem('Google returned a different account than requested. Nothing was saved; reconnect and choose the requested account.')
        if core.read_oauth_client_config(Path(record['oauth_client']))[1] != record['client_hash']:
            raise ConnectionProblem('The OAuth client changed during sign-in. Reconnect with the intended client.')
        profile = self._profile(record, actual)
        core.save_token_json(profile, credentials.to_json())
        record.update(status='connected', email=actual, profile=str(profile.path), verified_at=now())
        record.pop('authorization_url', None)
        self.save(record)

    def worker(self, connection_id, lock_fd):
        expected = self.root / 'locks' / (valid_id(connection_id) + '.gmail-auth')
        if os.fstat(lock_fd).st_ino != expected.stat().st_ino:
            raise ValueError('Invalid inherited OAuth worker lock')
        record = self.record(connection_id)
        try:
            dependencies = core.google_dependencies()
            owner = self

            class BrowserFlow(dependencies['InstalledAppFlow']):
                def authorization_url(self, **kwargs):
                    url, state = super().authorization_url(**kwargs)
                    record.update(status='awaiting_browser', authorization_url=url,
                        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
                    owner.save(record)
                    record['browser_launch_accepted'] = owner.launch_browser(url)
                    owner.save(record)
                    return url, state

            flow = BrowserFlow.from_client_secrets_file(record['oauth_client'], scopes=core.REQUIRED_SCOPES,
                                                       autogenerate_code_verifier=True)
            options = dict(access_type='offline', prompt='consent select_account')
            if record.get('requested_email'):
                options['login_hint'] = record['requested_email']
            flow.run_local_server(host='localhost', bind_addr='127.0.0.1', port=0, open_browser=False,
                timeout_seconds=600, authorization_prompt_message=None,
                success_message='Google sign-in returned. Return to Hermes and check Mailbridge connection status for verified account confirmation. No mail has been imported.', **options)
            record['status'] = 'verifying'
            record.pop('authorization_url', None)
            self.save(record)
            self.finish(record, flow, dependencies)
        except Exception as exc:
            record.pop('authorization_url', None)
            record['status'] = 'not_connected'
            record['error'] = str(exc) if isinstance(exc, ConnectionProblem) else {
                'WSGITimeoutError': 'Google sign-in timed out. Run connect Gmail again.',
                'AccessDeniedError': 'Google sign-in was denied. Review consent, test-user eligibility, or Workspace admin policy and reconnect.',
            }.get(type(exc).__name__, 'Google connection could not be verified or stored. No migration started. Check the local OAuth setup, browser consent, and test-user eligibility, then reconnect.')
            self.save(record)
        finally:
            os.close(lock_fd)

    def plan_profile(self, connection_id, source, source_mailbox, label_prefix='Mailbridge Archive'):
        record = self.record(connection_id)
        if record['status'] != 'connected':
            raise ValueError('Finish and verify Gmail sign-in before preparing this plan')
        if not source or not source_mailbox:
            raise ValueError('Provide the absolute EML source directory and source_mailbox for this connected Gmail account')
        source = Path(source).expanduser()
        if not source.is_absolute() or not source.is_dir():
            raise ValueError('Source must be an existing absolute EML directory')
        profile = core.load_json(Path(record['profile']))
        directory = self.root / 'gmail-plans' / uuid.uuid4().hex[:12]
        profile.update(source_root=str(source.resolve()), source_mailbox=source_mailbox,
                       state_dir=str(directory / 'engine-state'), label_prefix=label_prefix)
        path = directory / 'profile.json'
        core.write_json_atomic(path, profile, mode=0o600)
        core.load_profile(path)
        return str(path)
