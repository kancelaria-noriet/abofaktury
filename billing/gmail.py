"""Gmail API: a one-time OAuth connection and sending of messages (scope gmail.send only).

The OAuth client is of type "Web application". Google sends the browser back to
settings.GMAIL_REDIRECT_URI (https on a public domain; the same URI as in the client's settings).
"""

import base64
import json
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

from .models import GmailConnection

AUTHORIZE_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SEND_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
SCOPES = 'openid email https://www.googleapis.com/auth/gmail.send'
TIMEOUT = 30
REFRESH_MARGIN = timedelta(seconds=60)


class GmailError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class NotConnected(GmailError):
    """There is no valid connection. The operator must connect (again)."""


def client_config():
    """client_id and client_secret of the OAuth client (type "Web application"): GMAIL_CLIENT_JSON or the file."""
    try:
        if settings.GMAIL_CLIENT_JSON:
            data = json.loads(settings.GMAIL_CLIENT_JSON)
        elif settings.GMAIL_CLIENT_FILE:
            with open(settings.GMAIL_CLIENT_FILE, encoding='utf-8') as f:
                data = json.load(f)
        else:
            raise GmailError('brak klienta OAuth Google (GMAIL_CLIENT_JSON albo GMAIL_CLIENT_FILE)')
    except (OSError, ValueError) as e:
        raise GmailError(f'nie można wczytać klienta OAuth Google: {e}') from e
    config = data.get('installed') or data.get('web') or {}
    if not config.get('client_id') or not config.get('client_secret'):
        raise GmailError('plik klienta OAuth Google nie ma client_id lub client_secret')
    return config


def connection():
    return GmailConnection.objects.filter(pk=1).first()


def redirect_uri():
    if not settings.GMAIL_REDIRECT_URI:
        raise GmailError('brak adresu powrotu (GMAIL_REDIRECT_URI)')
    return settings.GMAIL_REDIRECT_URI


def authorize_url(state, challenge):
    params = {
        'client_id': client_config()['client_id'],
        'redirect_uri': redirect_uri(),
        'response_type': 'code',
        'scope': SCOPES,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'access_type': 'offline',
        'prompt': 'consent',
    }
    if settings.EMAIL_ACCOUNT:
        params['login_hint'] = settings.EMAIL_ACCOUNT
    return f'{AUTHORIZE_URL}?{urlencode(params)}'


def _token_request(data):
    config = client_config()
    data = {**data, 'client_id': config['client_id'], 'client_secret': config['client_secret']}
    try:
        response = requests.post(TOKEN_URL, data=data, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise GmailError(f'brak połączenia z Google: {e}') from e
    if response.status_code != 200:
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = body.get('error')
        message = body.get('error_description') or code or response.text[:200]
        raise GmailError(f'Google odrzucił żądanie tokenu ({response.status_code}): {message}', code=code)
    return response.json()


def _email_from_id_token(id_token):
    """The email in the ID token. The token comes straight from Google over TLS, so it is not verified again."""
    try:
        payload = id_token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get('email') or '')[:200]
    except (IndexError, ValueError, AttributeError):
        return ''


def _save(data, new=False):
    now = timezone.now()
    conn = connection()
    if conn is None or new:
        conn = GmailConnection(pk=1, connected_at=now)
    conn.access_token = data['access_token']
    if data.get('refresh_token'):
        conn.refresh_token = data['refresh_token']
    conn.expires_at = now + timedelta(seconds=int(data.get('expires_in', 3600)))
    if new:
        conn.account = _email_from_id_token(data.get('id_token', ''))
    conn.save()
    return conn


def exchange_code(code, verifier):
    data = _token_request({'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri(),
                           'code_verifier': verifier})
    if not data.get('refresh_token'):
        raise GmailError('Google nie przekazał tokenu odświeżania; połącz ponownie')
    return _save(data, new=True)


def access_token():
    conn = connection()
    if conn is None:
        raise NotConnected('Gmail nie jest połączony')
    if conn.expires_at > timezone.now() + REFRESH_MARGIN:
        return conn.access_token
    try:
        data = _token_request({'grant_type': 'refresh_token', 'refresh_token': conn.refresh_token})
    except GmailError as e:
        if e.code == 'invalid_grant':
            conn.delete()
            raise NotConnected('połączenie z Gmail wygasło, połącz ponownie') from e
        raise
    return _save(data).access_token


def disconnect():
    GmailConnection.objects.filter(pk=1).delete()


def send(message_bytes):
    """Send a complete MIME message. Return the Gmail message ID."""
    raw = base64.urlsafe_b64encode(message_bytes).decode('ascii')
    try:
        response = requests.post(SEND_URL, json={'raw': raw}, timeout=TIMEOUT,
                                 headers={'Authorization': f'Bearer {access_token()}'})
    except requests.RequestException as e:
        raise GmailError(f'brak połączenia z Gmail: {e}') from e
    if response.status_code == 401:
        raise NotConnected('Gmail odrzucił token (401), połącz ponownie')
    if response.status_code != 200:
        raise GmailError(f'Gmail nie wysłał wiadomości ({response.status_code}): {response.text[:300]}')
    return response.json().get('id', '')
