"""Source of the work entries.

The app reads the Google Sheet (settings.GOOGLE_SHEET_ID) through a read-only service account.
The result is {client_key: {"YYYY-MM": [raw entry, ...]}}. A raw entry is a dict with "description",
"hours", optional "expense" (the KOSZT column) and "cell" (A1 reference).

Error handling (PLAN.md 4.1):
- A problem with one tab (missing tab, no header row, a different layout) affects only that client.
- A problem with the whole sheet (network, key, Google error) raises SourceError.
"""

import json
from collections import Counter

from django.conf import settings

MONTHS = [
    'STYCZEŃ', 'LUTY', 'MARZEC', 'KWIECIEŃ', 'MAJ', 'CZERWIEC',
    'LIPIEC', 'SIERPIEŃ', 'WRZESIEŃ', 'PAŹDZIERNIK', 'LISTOPAD', 'GRUDZIEŃ',
]
TOTALS_LABELS = ('NADGODZINY', 'ILOŚĆ GODZIN')
HOURS_LABEL = 'ILOŚĆ GODZIN'
COST_LABEL = 'KOSZT'
HEADER_SEARCH_ROWS = 10       # the month header row must be in the first rows of the tab
READ_RANGE = 'A1:BZ120'       # the grid and the totals row; junk far below the grid is not read
SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']
API = 'https://sheets.googleapis.com/v4/spreadsheets'
TIMEOUT = 20


class SourceError(Exception):
    """The whole source failed. The page shows the message."""


class TabError(Exception):
    """One tab cannot be read. Only its client gets a warning."""


def load_entries(clients, year):
    """Return (entries, errors) for `clients` and the tab year `year`.

    entries: {client_key: {"YYYY-MM": [raw entry, ...]}}
    errors: {client_key: message} for the clients with a tab problem. The client keeps any entries
    that the parser could read.
    The app reads the source again for each request, so "Generuj ponownie" always shows current data.
    """
    if not settings.GOOGLE_SHEET_ID:
        raise SourceError('brak identyfikatora arkusza (GOOGLE_SHEET_ID)')
    return _load_sheet(clients, year)


# --- Google Sheets ------------------------------------------------------------------------------

def tab_name(client, year):
    return client.sheet_tab.replace('{rok}', str(year)).strip()


def _load_sheet(clients, year):
    session = _session()
    titles = _fetch_titles(session)

    entries, errors, wanted = {}, {}, {}
    for client in clients:
        name = tab_name(client, year)
        if not name:
            errors[client.key] = 'brak nazwy zakładki w konfiguracji klienta'
        elif name not in titles:
            errors[client.key] = f'brak zakładki „{name}” w arkuszu'
        else:
            wanted[client.key] = name

    if wanted:
        values = _fetch_values(session, sorted(set(wanted.values())))
        for key, name in wanted.items():
            try:
                entries[key], notes = parse_tab(name, values.get(name, []), year)
            except TabError as e:
                errors[key] = f'zakładka „{name}”: {e}'
            else:
                if notes:
                    errors[key] = f'zakładka „{name}”: ' + '; '.join(notes)
    return entries, errors


def _session():
    # Imported here, so that the demo mode and the tests do not need the Google libraries.
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    try:
        if settings.GOOGLE_SA_KEY_JSON:
            info = json.loads(settings.GOOGLE_SA_KEY_JSON)
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        elif settings.GOOGLE_SA_KEY_FILE:
            creds = service_account.Credentials.from_service_account_file(
                settings.GOOGLE_SA_KEY_FILE, scopes=SCOPES)
        else:
            raise SourceError('brak klucza konta usługi (GOOGLE_SA_KEY_FILE lub GOOGLE_SA_KEY_JSON)')
    except (OSError, ValueError) as e:
        raise SourceError(f'nie można wczytać klucza konta usługi: {e}') from e
    return AuthorizedSession(creds)


def _get(session, url, params):
    import google.auth.exceptions
    import requests

    try:
        response = session.get(url, params=params, timeout=TIMEOUT)
    except (requests.RequestException, google.auth.exceptions.GoogleAuthError) as e:
        raise SourceError(f'brak połączenia z Google Sheets: {e}') from e
    if response.status_code != 200:
        try:
            message = response.json()['error']['message']
        except (ValueError, KeyError, TypeError):
            message = response.text[:200]
        raise SourceError(f'Google Sheets zwrócił błąd {response.status_code}: {message}')
    return response.json()


def _fetch_titles(session):
    data = _get(session, f'{API}/{settings.GOOGLE_SHEET_ID}', {'fields': 'sheets.properties.title'})
    return {sheet['properties']['title'] for sheet in data.get('sheets', [])}


def _quote(name):
    return "'" + name.replace("'", "''") + "'"


def _fetch_values(session, names):
    params = [('ranges', f'{_quote(name)}!{READ_RANGE}') for name in names]
    params.append(('valueRenderOption', 'UNFORMATTED_VALUE'))
    data = _get(session, f'{API}/{settings.GOOGLE_SHEET_ID}/values:batchGet', params)
    ranges = data.get('valueRanges', [])
    # The response keeps the order of the requested ranges.
    return {name: value_range.get('values', []) for name, value_range in zip(names, ranges)}


# --- Parser (pure, no network) ------------------------------------------------------------------

def column_letter(index):
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ''
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _cell(row, col):
    if col is None or col >= len(row):
        return None
    value = row[col]
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _label(value):
    return str(value).strip().upper() if value is not None else ''


def parse_tab(name, rows, year):
    """Turn the rows of one tab (a list of lists, as the Sheets API returns them) into entries.

    Return ({"YYYY-MM": [raw entry, ...]}, notes). Raise TabError if the layout is not the expected
    month grid. The entries of a month end at its totals row. A month with no totals row uses the
    totals row of the other months. A tab with no totals row at all is read to the end, with a note.
    """
    header_index = next(
        (i for i, row in enumerate(rows[:HEADER_SEARCH_ROWS])
         if any(_label(value) in MONTHS for value in row)),
        None,
    )
    if header_index is None:
        raise TabError('nie znaleziono wiersza z nazwami miesięcy')
    header = rows[header_index]

    months = []
    for col, value in enumerate(header):
        label = _label(value)
        if label not in MONTHS:
            continue
        if not _label(_cell(header, col + 1)).startswith(HOURS_LABEL):
            raise TabError(f'{label.lower()}: brak kolumny „{HOURS_LABEL}” obok nazwy miesiąca')
        totals = next((i for i in range(header_index + 1, len(rows))
                       if _label(_cell(rows[i], col)).startswith(TOTALS_LABELS)), None)
        months.append((label, col, totals))

    found = Counter(totals for _, _, totals in months if totals is not None)
    tab_totals = found.most_common(1)[0][0] if found else None
    notes = []
    if tab_totals is None:
        notes.append('brak wiersza sumy (NADGODZINY / ILOŚĆ GODZIN), odczytano wszystkie wiersze')

    result = {}
    for label, col, totals in months:
        end = totals if totals is not None else tab_totals if tab_totals is not None else len(rows)
        cost_col = col + 2 if _label(_cell(header, col + 2)).startswith(COST_LABEL) else None
        month_key = f'{year:04d}-{MONTHS.index(label) + 1:02d}'
        result[month_key] = _parse_month(name, rows, header_index + 1, end, col, col + 1, cost_col)
    return result, notes


def _parse_month(name, rows, start, end, col, hours_col, cost_col):
    entries = []
    for index in range(start, end):
        row = rows[index]
        description = _cell(row, col)
        hours = _cell(row, hours_col)
        cost = _cell(row, cost_col)
        if description is None and hours is None and cost is None:
            continue
        description = '' if description is None else str(description)
        cell = f'{name}!{column_letter(col)}{index + 1}'
        if hours is not None:
            entries.append({'description': description, 'hours': hours, 'cell': cell})
        if cost is not None:
            entries.append({'description': description, 'hours': None, 'expense': cost, 'cell': cell})
        if hours is None and cost is None:
            entries.append({'description': description, 'hours': None, 'cell': cell})
    return entries
