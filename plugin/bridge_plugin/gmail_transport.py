"""Gmail REST facade over Hermes-owned OAuth. No credential or token store access."""
import base64
from types import SimpleNamespace
from urllib.parse import quote, urlencode

BASE = 'https://gmail.googleapis.com/gmail/v1/users/'
MAX_MESSAGE_BYTES = 25 * 1024 * 1024


class GmailHttpError(Exception):
    def __init__(self, status, content=b''):
        super().__init__(f'Gmail HTTP {status}')
        self.resp = SimpleNamespace(status=status)
        self.content = content


class Media:
    def __init__(self, stream, mimetype, resumable=False, chunksize=None):
        if mimetype != 'message/rfc822':
            raise ValueError('Only RFC822 message import is supported')
        self.raw = stream.read(MAX_MESSAGE_BYTES + 1)
        if not 1 <= len(self.raw) <= MAX_MESSAGE_BYTES:
            raise ValueError('Hermes JSON transport supports messages up to 25 MiB')


def host_request(provider, method, url, **kwargs):
    from hermes_cli.plugin_provider_requests import credentialed_provider_request, ProviderNotSignedIn
    try:
        return credentialed_provider_request(provider, method, url, **kwargs)
    except ProviderNotSignedIn:
        raise GmailHttpError(401, b'{"error":{"message":"Use hermes auth add mailbridge:google","errors":[{"reason":"authError"}]}}') from None


class Request:
    def __init__(self, method, path, query=None, body=None, account="me"):
        query = {k: ('true' if v else 'false') if isinstance(v, bool) else v
                 for k, v in (query or {}).items() if v is not None}
        self.method, self.body = method, body
        self.url = BASE + quote(account, safe='') + path + ('?' + urlencode(query, doseq=True) if query else '')

    def execute(self, num_retries=0):
        # Never retry writes here. The migration ledger reconciles ambiguous outcomes.
        response = host_request('google', self.method, self.url, json=self.body, timeout=120)
        if not 200 <= response.status < 300:
            raise GmailHttpError(response.status, response.body)
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError('Unexpected Gmail response shape')
        return result


def require_me(userId):
    if userId != 'me':
        raise ValueError('Only the current Hermes Google account is supported')


class Labels:
    def __init__(self, account):
        self.account = account

    def list(self, *, userId):
        require_me(userId)
        return Request('GET', '/labels', account=self.account)

    def create(self, *, userId, body):
        require_me(userId)
        return Request('POST', '/labels', body=body, account=self.account)


class Messages:
    def __init__(self, account):
        self.account = account

    def list(self, *, userId, q, includeSpamTrash, maxResults, pageToken=None, fields=None):
        require_me(userId)
        return Request('GET', '/messages', dict(q=q, includeSpamTrash=includeSpamTrash,
            maxResults=maxResults, pageToken=pageToken, fields=fields), account=self.account)

    def get(self, *, userId, id, format, metadataHeaders, fields):
        require_me(userId)
        if format != 'metadata':
            raise ValueError('Only metadata readback is supported')
        return Request('GET', '/messages/' + quote(str(id), safe=''),
            dict(format=format, metadataHeaders=metadataHeaders, fields=fields), account=self.account)

    def import_(self, *, userId, body, media_body, internalDateSource, neverMarkSpam,
                processForCalendar, deleted, fields):
        require_me(userId)
        if deleted or processForCalendar or internalDateSource != 'dateHeader':
            raise ValueError('Deletion and calendar processing are not supported')
        payload = {'labelIds': body['labelIds'],
                   'raw': base64.urlsafe_b64encode(media_body.raw).decode('ascii')}
        return Request('POST', '/messages/import', dict(internalDateSource=internalDateSource,
            neverMarkSpam=neverMarkSpam, processForCalendar=False, deleted=False, fields=fields), payload, account=self.account)


class GmailService:
    def __init__(self, account="me"):
        self.account = account

    def users(self):
        return self

    def getProfile(self, *, userId):
        require_me(userId)
        return Request('GET', '/profile', account=self.account)

    def labels(self):
        return Labels(self.account)

    def messages(self):
        return Messages(self.account)
