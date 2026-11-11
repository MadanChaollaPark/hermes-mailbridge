"""OAuth ownership, profile isolation and actual engine/REST adapter behavior."""
import ast
import base64
from contextlib import redirect_stdout, redirect_stderr
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlparse, parse_qs, unquote

import gmail_eml_migration_v2 as core
from bridge_plugin import gmail_transport as transport
from bridge_plugin.gmail_connection import GmailConnections
from bridge_plugin.service import Service, secret
import hermes_tools

ROOT = Path(__file__).resolve().parents[1]


def response(body, status=200):
    return SimpleNamespace(status=status, body=json.dumps(body).encode(), json=lambda: body)


class HermesAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.service = Service(self.root / 'runtime')
        self.conn = GmailConnections(self.service)
        self.scope = mock.patch.dict(os.environ, {'MAILBRIDGE_GOOGLE_CLIENT_ID':'fictional-client',
            'MAILBRIDGE_GOOGLE_CLIENT_SECRET':'fictional-client-secret'})
        self.scope.start()
        self.addCleanup(self.scope.stop)

    def profile(self):
        source = self.root / 'source'
        source.mkdir()
        for i in range(2):
            raw = f'Message-ID: <m{i}@example.test>\r\nDate: Mon, 02 Nov 2026 09:00:00 +0900\r\nFrom: sender@example.test\r\nTo: dest@example.test\r\nSubject: Fixture {i}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n안녕하세요 {i}\r\n'.encode()
            (source / f'{i}.eml').write_bytes(raw)
        (source/'copy.eml').write_bytes((source/'0.eml').read_bytes())
        data = json.loads((ROOT/'examples/profile.example.json').read_text())
        data.update(source_root=str(source),state_dir=str(self.root/'state'),destination='dest@example.test',
                    source_mailbox='old@example.test')
        path=self.root/'profile.json'
        path.write_text(json.dumps(data))
        return core.load_profile(path)

    def test_missing_signin_returns_human_command_without_credentials(self):
        with mock.patch.object(transport, 'host_request', side_effect=transport.GmailHttpError(401)):
            result = self.conn.connect()
        self.assertEqual(result['status'],'sign_in_required')
        self.assertEqual(result['sign_in_command'],'hermes auth add mailbridge:google')
        self.assertEqual(self.conn.status()['connections'],[])
        self.assertNotIn('fictional-client-secret',json.dumps(result))

    def test_account_choice_is_verified_and_no_token_is_saved(self):
        with mock.patch.object(transport,'host_request',return_value=response({'emailAddress':'dest@example.test'})):
            result=self.conn.connect(email='dest@example.test')
        self.assertEqual(result['email'],'dest@example.test')
        self.assertEqual(result['credential_owner'],'Hermes')
        stored=self.service.load('gmail-connections',result['connection_id'])
        self.assertEqual(set(stored),{'connection_id','status','email','verified_at','auth_provider'})
        self.assertFalse(list(self.root.rglob('*token*')))

    def test_wrong_account_never_creates_binding(self):
        with mock.patch.object(transport,'host_request',return_value=response({'emailAddress':'other@example.test'})):
            with self.assertRaises(ValueError):
                self.conn.connect(email='dest@example.test')
        self.assertEqual(self.conn.status()['connections'],[])

    def test_profiles_refuse_legacy_credential_files_before_reading_them(self):
        profile=self.profile()
        data=json.loads(profile.path.read_text())
        for extra in ({'oauth_client':'/private/credential.json'}, {'token_store':{'type':'file','path':'/private/token.json'}}):
            with self.subTest(extra=extra):
                profile.path.write_text(json.dumps(data | extra))
                with self.assertRaisesRegex(core.MigrationError,'Hermes-managed'):
                    core.load_profile(profile.path)

    def test_plugin_does_not_implement_token_storage_or_browser_consent(self):
        self.assertFalse(hasattr(core,'load_token_json'))
        self.assertFalse(hasattr(core,'save_token_json'))
        self.assertFalse(hasattr(self.conn,'worker'))
        with self.assertRaisesRegex(core.AuthRequired,'hermes auth add'):
            core.command_authorize(self.profile())

    def test_profile_scoped_secret_wins_and_unrelated_secrets_do_not_escape(self):
        from agent.secret_scope import set_secret_scope, reset_secret_scope
        scope=set_secret_scope({'MAILBRIDGE_POP_PASSWORD':'right-profile-secret'})
        self.addCleanup(reset_secret_scope,scope)
        with mock.patch.dict(os.environ,{'MAILBRIDGE_POP_PASSWORD':'wrong-global-secret','UNRELATED_API_KEY':'never-forward'}):
            with mock.patch.object(hermes_tools.subprocess,'run',return_value=SimpleNamespace(stdout='{"ok":true}')) as run:
                hermes_tools.invoke('capture',{'connection_id':'source'},self.root/'profile/plugin-data/mailbridge')
            env=run.call_args.kwargs['env']
        self.assertEqual(env['MAILBRIDGE_POP_PASSWORD'],'right-profile-secret')
        self.assertNotIn('UNRELATED_API_KEY',env)
        self.assertEqual(env['HERMES_HOME'],str(self.root/'profile'))
        with self.assertRaises(ValueError): secret('UNRELATED_API_KEY')

    def test_utf8_import_is_exact_json_with_no_write_retries(self):
        raw=b'From: a@example.test\r\n\r\n'+ '안녕하세요'.encode()
        media=transport.Media(io.BytesIO(raw),'message/rfc822')
        request=transport.GmailService('dest@example.test').messages().import_(userId='me',
            body={'labelIds':['INBOX']}, media_body=media, internalDateSource='dateHeader',
            neverMarkSpam=True,processForCalendar=False,deleted=False,fields='id')
        with mock.patch.object(transport,'host_request',side_effect=TimeoutError('accepted but response lost')) as host:
            with self.assertRaises(TimeoutError): request.execute(num_retries=8)
        self.assertEqual(host.call_count,1)
        self.assertEqual(base64.urlsafe_b64decode(host.call_args.kwargs['json']['raw']),raw)
        self.assertIn('/users/dest%40example.test/messages/import',host.call_args.args[2])

    def test_import_is_bound_to_explicit_account_even_if_signin_changes(self):
        service=transport.GmailService('dest@example.test')
        with mock.patch.object(transport,'host_request',return_value=response({'error':{'message':'wrong user'}},403)) as host:
            with self.assertRaises(transport.GmailHttpError):
                service.getProfile(userId='me').execute()
        self.assertIn('/users/dest%40example.test/profile',host.call_args.args[2])
        self.assertNotIn('/users/me/',host.call_args.args[2])

    def test_message_size_limit_stops_before_network(self):
        with mock.patch.object(transport,'MAX_MESSAGE_BYTES',10),mock.patch.object(transport,'host_request') as host:
            with self.assertRaises(ValueError): transport.Media(io.BytesIO(b'x'*11),'message/rfc822')
        host.assert_not_called()

    def test_engine_and_host_adapter_resume_lost_response_without_duplicate_import(self):
        profile=self.profile()
        stored={}
        imports=[]
        lose_first=[True]
        visible=[False]
        def api(provider,method,url,**kwargs):
            self.assertEqual(provider,'google')
            self.assertTrue(url.startswith('https://gmail.googleapis.com/gmail/v1/users/dest%40example.test/'))
            parsed=urlparse(url);query=parse_qs(parsed.query);path=parsed.path
            if path.endswith('/profile'): return response({'emailAddress':'dest@example.test'})
            if path.endswith('/labels'): return response({'labels':[{'id':'INBOX','name':'INBOX'}]})
            if path.endswith('/messages/import'):
                raw=base64.urlsafe_b64decode(kwargs['json']['raw']);message=BytesParser(policy=policy.default).parsebytes(raw)
                gid='g'+str(len(imports)+1);imports.append(raw)
                stored[gid]={'id':gid,'threadId':gid,'labelIds':kwargs['json']['labelIds'],
                    'internalDate':str(int(parsedate_to_datetime(message['Date']).timestamp()*1000)),
                    'payload':{'headers':[{'name':k,'value':str(message[k])} for k in ('Message-ID','Date','From','To','Subject')]}}
                if lose_first.pop() if lose_first else False: raise TimeoutError('lost ack')
                return response(stored[gid])
            if path.endswith('/messages'):
                if not visible[0]: return response({'messages':[]})
                querytext=query['q'][0]
                matches=[{'id':gid} for gid,msg in stored.items() if any(h['name']=='Message-ID' and h['value'].strip('<>') in querytext for h in msg['payload']['headers'])]
                return response({'messages':matches})
            if '/messages/' in path: return response(stored[unquote(path.rsplit('/',1)[1])])
            raise AssertionError((method,url))
        with redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()),mock.patch.object(transport,'host_request',side_effect=api),mock.patch.object(core.time,'sleep'):
            core.command_prepare(profile,False)
            with self.assertRaises(core.RunBlocked): core.command_run(profile,profile.destination,3)
            self.assertEqual(len(imports),1)
            visible[0]=True
            core.command_run(profile,profile.destination,3)
            self.assertEqual(len(imports),2)
            core.command_run(profile,profile.destination,3)
            self.assertEqual(len(imports),2)
        report=core.load_json(profile.report_path)
        self.assertTrue(report['gmail_complete'])
        self.assertEqual(report['gmail_covered_source_rows'],3)
        self.assertEqual(set(imports),{p.read_bytes() for p in profile.source_root.glob('*.eml')})


if __name__=='__main__': unittest.main()
