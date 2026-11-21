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
    """ClientId, ClientSecret and SubscriptionKey: from the env vars, or from SUBIEKT_CREDENTIALS_FILE."""
    from_env = {'ClientId': settings.SUBIEKT_CLIENT_ID, 'ClientSecret': settings.SUBIEKT_CLIENT_SECRET,
                'SubscriptionKey': settings.SUBIEKT_SUBSCRIPTION_KEY}
    if any(from_env.values()):
        missing = [name for name, key in (('SUBIEKT_CLIENT_ID', 'ClientId'), ('SUBIEKT_CLIENT_SECRET', 'ClientSecret'),
                                          ('SUBIEKT_SUBSCRIPTION_KEY', 'SubscriptionKey')) if not from_env[key]]
        if missing:
            raise SubiektError('brak zmiennych: ' + ', '.join(missing))
        return from_env
    path = settings.SUBIEKT_CREDENTIALS_FILE
    if not path:
        raise SubiektError('brak danych dostępowych (SUBIEKT_CLIENT_ID itd. albo SUBIEKT_CREDENTIALS_FILE)')
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

class NotFound(SubiektError):
    """The API answered 404."""


def _error_text(response):
    """A short text from an API error: the validation messages if there are any."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, list):   # [{"code", "message", "path"}, ...]
        return '; '.join(f"{item.get('path') or ''}: {item.get('message') or item.get('code')}" for item in body)[:600]
    if isinstance(body, dict) and body.get('errors'):
        return '; '.join(f'{key}: {" ".join(map(str, value))}' for key, value in body['errors'].items())[:600]
    return str(body)[:300]


def _api(method, path, params=None, body=None, raw=False):
    """Call the Subiekt 123 API. Return the JSON answer, None for an empty answer (204), or the bytes (raw)."""
    headers = {
        'Authorization': f'Bearer {access_token()}',
        'Ocp-Apim-Subscription-Key': credentials()['SubscriptionKey'],
        'Accept': 'application/json',
        'x-api-version': API_VERSION,
    }
    url = settings.SUBIEKT_API_BASE.rstrip('/') + path
    try:
        response = requests.request(method, url, params=params, json=body, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise SubiektError(f'brak połączenia z Subiekt 123: {e}') from e
    if response.status_code == 401:
        raise NotConnected('Subiekt odrzucił token (401), połącz ponownie')
    if response.status_code == 404:
        raise NotFound(f'Subiekt 123: nie znaleziono ({path})')
    if response.status_code not in (200, 201, 204):
        raise SubiektError(f'Subiekt 123 zwrócił błąd {response.status_code}: {_error_text(response)}')
    if raw:
        return response.content
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as e:
        raise SubiektError(f'Subiekt 123 zwrócił odpowiedź, która nie jest JSON: {response.text[:200]}') from e


def api_get(path, params=None):
    return _api('GET', path, params=params)


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


# --- Invoices (step 2) ---------------------------------------------------------------------------

# Each invoice line uses a catalogue product, found by symbol and type Service (PLAN.md 4.3).
PRODUCT_SYMBOLS = {
    'usługa prawna - abonament': '69.10.1 abonament',
    'usługa prawna - nadgodziny': '69.10.1 nadgodziny',
    'usługa prawna': '69.10.1',
    'usługa prawna - koszty zastępstwa': '69.10.1 kzp',
    'usługa prawna - opłata': '69.10.1 opłata',
}
VAT_RATE_TEXT = '23%'
DUE_DAYS = 7


def list_products(page_size=MAX_PAGE_SIZE, max_pages=100):
    products = []
    for page in range(max_pages):
        items, total = _page(api_get('/products', {'pageNumber': page, 'pageSize': page_size}))
        products.extend(items)
        if len(items) < page_size or (total is not None and len(products) >= total):
            break
    return products


def find_products(products, symbols):
    """Return {symbol: product} for the symbols. Each symbol must have exactly one Service product."""
    found, problems = {}, []
    for symbol in symbols:
        hits = [p for p in products
                if (p.get('symbol') or '').strip() == symbol and p.get('kind') == 'Service']
        if len(hits) == 1:
            found[symbol] = hits[0]
        elif not hits:
            problems.append(f'brak produktu (usługi) o symbolu „{symbol}”')
        else:
            problems.append(f'kilka produktów (usług) o symbolu „{symbol}”')
    if problems:
        raise SubiektError('; '.join(problems))
    return found


def find_client(subiekt_clients, nip):
    """The one Subiekt client with this NIP (digits compared). A missing or double match is an error."""
    digits = nip_digits(nip)
    if not digits:
        raise SubiektError('klient nie ma NIP w konfiguracji aplikacji')
    hits = [item for item in subiekt_clients if nip_digits(item.get('tin')) == digits]
    if not hits:
        raise SubiektError(f'brak kontrahenta w Subiekcie z NIP {digits}; dodaj go w Subiekcie')
    if len(hits) > 1:
        raise SubiektError(f'kilku kontrahentów w Subiekcie z NIP {digits}')
    return hits[0]


def _date(value):
    return f'{value.isoformat()}T00:00:00'


def build_invoice(result, subiekt_client, products, issuer, issue_date, place=''):
    """The body of POST /documents for one calculated invoice (calc.Result). Pure, no API calls.

    The settings copy the practice of the accounting firm: a KSeF invoice, bank transfer, net
    calculation, a domestic sale (SK), sale date = tax date = issue date, payment due in 7 days.
    The API requires the "payments" field (a missing field is an error, an empty list is accepted). A KZP
    "tylko VAT" adds a payment of its net amount, so only the VAT is left to pay.
    """
    if not result.lines:
        raise SubiektError('faktura nie ma pozycji')
    if not issuer:
        raise SubiektError('brak wystawcy faktury (SUBIEKT_ISSUER)')
    items = []
    for number, line in enumerate(result.lines, 1):
        symbol = PRODUCT_SYMBOLS.get(line.name)
        if symbol is None:
            raise SubiektError(f'brak produktu dla pozycji „{line.name}”')
        items.append({
            'no': number,
            'productName': line.name,
            'sourceProductId': products[symbol]['id'],
            'quantity': float(line.quantity),
            'unit': line.unit,
            'netPrice': float(line.unit_net),
            'vatRate': VAT_RATE_TEXT,
        })
    client = {key: subiekt_client.get(key) for key in ('name', 'tin', 'kind', 'tinKind', 'address')}
    client['sourceClientId'] = subiekt_client['id']
    body = {
        'kind': 'Invoice',
        'eInvoice': True,
        'currency': 'PLN',
        'issuer': issuer,
        'calculationMethod': 'Net',
        'paymentMethod': 'BankTransfer',
        'splitPayment': 'False',
        'communityTransaction': 'SK',
        'issueDate': _date(issue_date),
        'deliveryDate': _date(issue_date),
        'taxLiabilityDate': _date(issue_date),
        'dueDate': _date(issue_date + timedelta(days=DUE_DAYS)),
        'client': client,
        'items': items,
    }
    if place:
        body['transactionLocation'] = place
    body['payments'] = []
    if result.paid:
        body['payments'].append({'date': _date(issue_date), 'onDocument': True, 'value': float(result.paid)})
    return body


def create_document(body):
    """POST /documents. Return the ID of the new document."""
    data = _api('POST', '/documents', body=body)
    document_id = (data or {}).get('id')
    if isinstance(document_id, dict):
        document_id = document_id.get('value')
    if not document_id:
        raise SubiektError(f'Subiekt 123 nie zwrócił identyfikatora dokumentu: {str(data)[:200]}')
    return str(document_id)


def get_document(document_id):
    """GET /documents/{id}, or None if the document does not exist (any more)."""
    try:
        return api_get(f'/documents/{document_id}')
    except NotFound:
        return None


def delete_document(document_id):
    _api('DELETE', f'/documents/{document_id}')


def document_id_by_number(number):
    """The ID of the document with this number (exact match), for example "FS 29/10/2026"."""
    items, _ = _page(api_get('/documents', {'pageNumber': 0, 'pageSize': MAX_PAGE_SIZE,
                                            'filters': f'documentNumber={number}'}))
    hits = [item for item in items if item.get('documentNumber') == number]
    if len(hits) != 1:
        raise SubiektError(f'nie znaleziono w Subiekcie jednego dokumentu o numerze {number}')
    return hits[0]['id']


def document_pdf(document_id):
    """The PDF of a document (the original, not a duplicate)."""
    pdf = _api('POST', f'/documents/{document_id}/printing', body={'duplicateDate': None, 'ecoMode': False}, raw=True)
    if not pdf or not pdf.startswith(b'%PDF'):
        raise SubiektError(f'Subiekt 123 nie zwrócił pliku PDF: {pdf[:100]!r}')
    return pdf


SUBSCRIPTION_LINE = 'usługa prawna - abonament'


def documents_issued_between(first_day, next_first_day, max_pages=100):
    """The documents with an issue date in [first_day, next_first_day), newest first."""
    first, end = first_day.isoformat(), next_first_day.isoformat()
    found = []
    for page in range(max_pages):
        items, _ = _page(api_get('/documents', {'pageNumber': page, 'pageSize': MAX_PAGE_SIZE,
                                                'orderBy': 'issueDate:desc'}))
        found += [item for item in items if first <= str(item.get('issueDate'))[:10] < end]
        if len(items) < MAX_PAGE_SIZE or (items and str(items[-1].get('issueDate'))[:10] < first):
            break
    return found


def client_invoices(documents, nip):
    """The invoices of the client (by NIP) in a list of documents, with the names of their lines."""
    digits = nip_digits(nip)
    invoices = []
    for item in documents:
        if item.get('kind') != 'Invoice' or not digits or nip_digits(item.get('clientTin')) != digits:
            continue
        document = get_document(item['id']) or {}
        lines = [line.get('productName') for line in document.get('items') or []]
        invoices.append({
            'number': item.get('documentNumber'),
            'date': str(item.get('issueDate'))[:10],
            'net': item.get('totalNet'),
            'ksef': bool(item.get('ksefNumber')),
            'lines': lines,
            'has_subscription': SUBSCRIPTION_LINE in lines,
        })
    return invoices
