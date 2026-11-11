"""Nonsecret account binding; Hermes alone performs OAuth sign-in and refresh."""
import json
from pathlib import Path
import uuid
import gmail_eml_migration_v2 as core
from bridge_plugin.gmail_transport import GmailService, GmailHttpError
from bridge_plugin.service import valid_id
from mailbridge.store import now

AUTH_COMMAND = 'hermes auth add mailbridge:google'


class GmailConnections:
    def __init__(self, service):
        self.service = service
        self.root = service.root

    def record(self, cid):
        return self.service.load('gmail-connections', valid_id(cid))

    def status(self, connection_id=None):
        if connection_id is None:
            return {'connections': [self.status(p.stem) for p in sorted(
                (self.root / 'gmail-connections').glob('*.json'))],
                'sign_in_command': AUTH_COMMAND, 'credential_owner': 'Hermes'}
        record = self.record(connection_id)
        return {k: record[k] for k in ('connection_id', 'status', 'email', 'verified_at')} | {
            'credential_owner': 'Hermes', 'migration_started': False,
            'verification_scope': 'Account identity at verified_at; run rechecks the current identity before writes.'}

    def connect(self, email=None):
        if email is not None:
            email = core.normalize_mailbox(email, 'email')
        from agent.secret_scope import get_secret
        if not get_secret("MAILBRIDGE_GOOGLE_CLIENT_ID") or not get_secret("MAILBRIDGE_GOOGLE_CLIENT_SECRET"):
            return {'status': 'setup_required', 'credential_owner': 'Hermes',
                'sign_in_command': AUTH_COMMAND,
                'next_action': 'Configure the Google Desktop client ID and secret in Hermes Settings > Plugins > Mailbridge, then run the sign-in command in your own terminal. Do not paste credentials in chat.'}
        try:
            profile = GmailService().users().getProfile(userId='me').execute()
        except GmailHttpError as exc:
            if exc.resp.status == 401:
                return {'status': 'sign_in_required', 'sign_in_command': AUTH_COMMAND,
                    'credential_owner': 'Hermes', 'migration_started': False,
                    'next_action': 'The user runs this command in their own local terminal. Hermes opens consent and stores the grant. Then call connect again. Never run sign-in from the agent, gateway or cron.'}
            raise
        actual = core.normalize_mailbox(profile.get('emailAddress'), 'Google account')
        if email and actual != email:
            raise ValueError('Hermes Google account differs from the requested destination; change the Hermes sign-in and retry. No binding or import was created.')
        cid = 'gmail-' + uuid.uuid4().hex[:12]
        self.service.save('gmail-connections', cid, dict(connection_id=cid,
            status='connected', email=actual, verified_at=now(), auth_provider='google'))
        return self.status(cid)

    def plan_profile(self, connection_id, source, source_mailbox, label_prefix='Mailbridge Archive'):
        record = self.record(connection_id)
        if record['status'] != 'connected':
            raise ValueError('Verify the Hermes Google account first')
        if not source or not source_mailbox:
            raise ValueError('Provide source and source_mailbox')
        source = Path(source).expanduser()
        if not source.is_absolute() or not source.is_dir():
            raise ValueError('Source must be an existing absolute EML directory')
        directory = self.root / 'gmail-plans' / uuid.uuid4().hex[:12]
        profile = dict(schema_version=1, migration_id=directory.name,
            source_root=str(source.resolve()), source_mailbox=source_mailbox, destination=record['email'],
            state_dir=str(directory / 'engine-state'), auth_provider='google', token_store={'type':'hermes'},
            label_prefix=label_prefix, custom_label_policy='archive_prefix_and_folder', root_folder='Inbox',
            inbox_folders=['Inbox'], google_rejection_policy='stop', missing_message_id_policy='stop')
        path = directory / 'profile.json'
        core.write_json_atomic(path, profile, mode=0o600)
        core.load_profile(path)
        return str(path)
