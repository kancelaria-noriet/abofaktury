"""Test helpers: a fake Google Sheet with invented data. The tests need no network and no key."""

from unittest import mock

from django.test import override_settings

from billing import source

SHEET_SETTINGS = {'GOOGLE_SHEET_ID': 'test-sheet', 'GOOGLE_SA_KEY_FILE': '/nonexistent/key.json'}


def make_tab(entries_by_month, cost_column=True):
    """Build the rows of a tab with all 12 months, like the real sheet.

    entries_by_month: {month number: [(description, hours), (description, hours, cost), ...]}.
    Each month has the columns MIESIĄC | ILOŚĆ GODZIN (| KOSZT), and a totals row below the entries.
    """
    width = 3 if cost_column else 2
    header = []
    for name in source.MONTHS:
        header += [name, 'ILOŚĆ GODZIN'] + (['KOSZT'] if cost_column else [])
    rows = [header]
    depth = max((len(items) for items in entries_by_month.values()), default=0)
    for index in range(depth):
        row = []
        for month in range(1, 13):
            items = entries_by_month.get(month, [])
            if index < len(items):
                description, hours, *cost = items[index]
                row += [description, '' if hours is None else hours]
                if cost_column:
                    row.append(cost[0] if cost else '')
            else:
                row += [''] * width
        rows.append(row)
    totals = []
    for _ in range(12):
        totals += ['NADGODZINY:', ''] + ([''] if cost_column else [])
    rows.append(totals)
    return rows


class FakeSheetMixin:
    """Replace the Google calls with the tabs in self.tabs ({tab name: rows})."""

    tabs = {}

    def setUp(self):
        super().setUp()
        settings = override_settings(**SHEET_SETTINGS)
        settings.enable()
        self.addCleanup(settings.disable)
        patches = [
            mock.patch.object(source, '_session', return_value=object()),
            mock.patch.object(source, '_fetch_titles', side_effect=lambda session: set(self.tabs)),
            mock.patch.object(source, '_fetch_values',
                              side_effect=lambda session, names: {name: self.tabs[name] for name in names}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
