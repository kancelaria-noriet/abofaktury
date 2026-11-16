"""Subiekt 123 API (InsERT): the OAuth connection and read-only calls.

Step 1 of the integration (PLAN.md P9): connect once (OAuth 2.0 authorization code with PKCE), keep
the tokens in the database, refresh them, and read the client list to check the NIPs. This module
creates nothing in Subiekt.

Every API request needs two things: the access token (Authorization: Bearer) and the subscription
key (Ocp-Apim-Subscription-Key).
"""

import base64
import hashlib
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils import timezone

from .models import SubiektConnection

AUTHORIZE_URL = 'https://kontoapi.insert.com.pl/connect/authorize'
TOKEN_URL = 'https://kontoapi.insert.com.pl/connect/token'
USERINFO_URL = 'https://kontoapi.insert.com.pl/connect/userinfo'
SCOPES = 'openid profile email offline_access subiekt123'
TIMEOUT = 20
REFRESH_MARGIN = timedelta(seconds=60)
REQUIRED_KEYS = ('ClientId', 'ClientSecret', 'SubscriptionKey')
API_VERSION = '1.1'          # the x-api-version header, required by API 1.1
MAX_PAGE_SIZE = 50           # the API rejects a larger pageSize (PAGE_SIZE_TOO_BIG)


class SubiektError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class NotConnected(SubiektError):
    """There is no valid connection. The operator must connect (again)."""


def credentials():
    """Read ClientId, ClientSecret and SubscriptionKey from settings.SUBIEKT_CREDENTIALS_FILE."""
    path = settings.SUBIEKT_CREDENTIALS_FILE
    if not path:
        raise SubiektError('brak pliku z danymi dostępowymi (SUBIEKT_CREDENTIALS_FILE)')
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError as e:
        raise SubiektError(f'nie można wczytać pliku z danymi dostępowymi: {e}') from e
    values = {}
    for line in lines:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    missing = [key for key in REQUIRED_KEYS if not values.get(key)]
    if missing:
        raise SubiektError('brak w pliku z danymi dostępowymi: ' + ', '.join(missing))
    return values


def redirect_uri():
    if not settings.SUBIEKT_REDIRECT_URI:
        raise SubiektError('brak adresu powrotu (SUBIEKT_REDIRECT_URI)')
    return settings.SUBIEKT_REDIRECT_URI


def connection():
    return SubiektConnection.objects.filter(pk=1).first()


# --- OAuth ---------------------------------------------------------------------------------------

def new_pkce():
    """Return (verifier, challenge) for PKCE with S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return verifier, challenge


def authorize_url(state, challenge):
    params = {
        'client_id': credentials()['ClientId'],
        'response_type': 'code',
        'redirect_uri': redirect_uri(),
        'scope': SCOPES,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
    }
    return f'{AUTHORIZE_URL}?{urlencode(params)}'


def _token_request(data):
    creds = credentials()
    # client_secret_post: the secret goes in the form body. With HTTP Basic the server URL-decodes the
    # credentials, so a "+" in the secret would become a space (invalid_client).
    data = {**data, 'client_id': creds['ClientId'], 'client_secret': creds['ClientSecret']}
    try:
        response = requests.post(TOKEN_URL, data=data, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise SubiektError(f'brak połączenia z InsERT: {e}') from e
    if response.status_code != 200:
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = body.get('error')
        message = body.get('error_description') or code or response.text[:200]
        raise SubiektError(f'InsERT odrzucił żądanie tokenu ({response.status_code}): {message}', code=code)
    return response.json()


def _account_name(access_token):
    """The email or name of the InsERT account, for the status line. Empty if not available."""
    try:
        response = requests.get(USERINFO_URL, timeout=TIMEOUT,
                                headers={'Authorization': f'Bearer {access_token}'})
        info = response.json() if response.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        info = {}
    return str(info.get('email') or info.get('name') or '')[:200]


def _save(data, new=False):
    now = timezone.now()
    conn = connection()
    if conn is None or new:
        conn = SubiektConnection(pk=1, connected_at=now)
    conn.access_token = data['access_token']
    if data.get('refresh_token'):
        conn.refresh_token = data['refresh_token']
    conn.expires_at = now + timedelta(seconds=int(data.get('expires_in', 3600)))
    if new:
        conn.account = _account_name(conn.access_token)
    conn.save()
    return conn


def exchange_code(code, verifier):
    """Finish the connection: trade the authorization code for tokens and store them."""
    data = _token_request({
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': redirect_uri(),
        'code_verifier': verifier,
    })
    return _save(data, new=True)


def access_token():
    """Return a valid access token. Refresh it when it expires soon."""
    conn = connection()
    if conn is None:
        raise NotConnected('Subiekt nie jest połączony')
    if conn.expires_at > timezone.now() + REFRESH_MARGIN:
        return conn.access_token
    if not conn.refresh_token:
        raise NotConnected('token wygasł, połącz ponownie z Subiektem')
    try:
        data = _token_request({'grant_type': 'refresh_token', 'refresh_token': conn.refresh_token})
    except SubiektError as e:
        if e.code == 'invalid_grant':
            conn.delete()
            raise NotConnected('połączenie wygasło, połącz ponownie z Subiektem') from e
        raise
    return _save(data).access_token


def disconnect():
    SubiektConnection.objects.filter(pk=1).delete()


# --- API (read-only) ----------------------------------------------------------------------------

def api_get(path, params=None):
    headers = {
        'Authorization': f'Bearer {access_token()}',
        'Ocp-Apim-Subscription-Key': credentials()['SubscriptionKey'],
        'Accept': 'application/json',
        'x-api-version': API_VERSION,
    }
    url = settings.SUBIEKT_API_BASE.rstrip('/') + path
    try:
        response = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise SubiektError(f'brak połączenia z Subiekt 123: {e}') from e
    if response.status_code == 401:
        raise NotConnected('Subiekt odrzucił token (401), połącz ponownie')
    if response.status_code != 200:
        raise SubiektError(f'Subiekt 123 zwrócił błąd {response.status_code}: {response.text[:300]}')
    try:
        return response.json()
    except ValueError as e:
        raise SubiektError(f'Subiekt 123 zwrócił odpowiedź, która nie jest JSON: {response.text[:200]}') from e


def _page(data):
    """Return (items, total) of one list page. API 1.1: {"items": [...], "totalItemsCount": n}."""
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict) and isinstance(data.get('items'), list):
        return data['items'], data.get('totalItemsCount')
    raise SubiektError(f'nieznany format listy: {str(data)[:200]}')


def list_clients(page_size=MAX_PAGE_SIZE, max_pages=100):
    """All clients in Subiekt, page by page. The API counts the pages from 0."""
    clients = []
    for page in range(max_pages):
        items, total = _page(api_get('/clients', {'pageNumber': page, 'pageSize': page_size}))
        clients.extend(items)
        if len(items) < page_size or (total is not None and len(clients) >= total):
            break
    return clients


def nip_digits(value):
    """Only the digits of a NIP ("PL 123‐456‐78‐90" -> "1234567890")."""
    return re.sub(r'\D', '', str(value or ''))


def match_clients(app_clients, subiekt_clients):
    """Return [(client, nip, [matching Subiekt clients])] for the app clients, matched by NIP."""
    by_nip = {}
    for item in subiekt_clients:
        nip = nip_digits(item.get('tin'))
        if nip:
            by_nip.setdefault(nip, []).append(item)
    rows = []
    for client in app_clients:
        nip = nip_digits(client.nip)
        rows.append((client, nip, by_nip.get(nip, []) if nip else []))
    return rows
