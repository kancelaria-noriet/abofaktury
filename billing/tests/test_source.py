from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase, mock

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase, override_settings
from django.urls import reverse

from billing import source
from billing.calc import KZP_FULL, KZP_VAT_ONLY, Month, calculate, parse_entry
from billing.source import SourceError, TabError, column_letter, parse_tab
from billing.tests.helpers import FakeSheetMixin


def grid(*rows):
    return [list(row) for row in rows]


# A tab like Alfa_2026: it starts in July, October has a KOSZT column.
TAB = grid(
    ['LIPIEC', 'ILOŚĆ GODZIN', 'PAŹDZIERNIK', 'ILOŚĆ GODZIN', 'KOSZT'],
    ['weryfikacja NDA', 4, 'spotkanie', '', ''],
    ['  umowa  ', 2.5, 'wniosek o uzasadnienie', 1, 100],
    ['', '', 'opłata kancelaryjna', '', 20],
    ['', '', 'KZP KOWALSKI + VAT (DO ZAPŁATY JEDYNIE VAT)', '', 1009.21],
    ['NADGODZINY:', 1, 'NADGODZINY:', -4, ''],
    ['notatka pod tabelą', 16],   # junk below the totals row
)


class ParseTabTests(TestCase):
    def test_months_and_entries(self):
        months, notes = parse_tab('Alfa_2026', TAB, 2026)
        self.assertEqual(notes, [])
        self.assertEqual(sorted(months), ['2026-07', '2026-10'])
        self.assertEqual(months['2026-07'], [
            {'description': 'weryfikacja NDA', 'hours': 4, 'cell': 'Alfa_2026!A2'},
            {'description': 'umowa', 'hours': 2.5, 'cell': 'Alfa_2026!A3'},
        ])

    def test_hours_and_cost_in_one_row_give_two_entries(self):
        october = parse_tab('T', TAB, 2026)[0]['2026-10']
        self.assertEqual(october[0], {'description': 'spotkanie', 'hours': None, 'cell': 'T!C2'})
        self.assertEqual(october[1], {'description': 'wniosek o uzasadnienie', 'hours': 1, 'cell': 'T!C3'})
        self.assertEqual(october[2], {'description': 'wniosek o uzasadnienie', 'hours': None,
                                      'expense': 100, 'cell': 'T!C3'})
        self.assertEqual(october[3]['expense'], 20)
        self.assertEqual(len(october), 5)   # the junk below the totals row is not read

    def test_header_below_the_first_row(self):
        rows = grid(['NAGŁÓWEK'], ['SIERPIEŃ', 'ILOŚĆ GODZIN'], ['konsultacja', 1], ['NADGODZINY:', -7])
        self.assertEqual(parse_tab('Klient_2026', rows, 2026)[0]['2026-08'][0]['cell'], 'Klient_2026!A3')

    def test_no_month_header_is_a_tab_error(self):
        rows = grid(['CZYNNOSC', 'ILOŚĆ GODZIN', 'DATA'], ['konsultacja', 1, 'sierpień 2023'])
        with self.assertRaisesRegex(TabError, 'nie znaleziono wiersza z nazwami miesięcy'):
            parse_tab('Inny_uklad', rows, 2026)

    def test_month_without_totals_uses_the_totals_row_of_the_tab(self):
        rows = grid(['STYCZEŃ', 'ILOŚĆ GODZIN', 'LUTY', 'ILOŚĆ GODZIN'],
                    ['a', 1, 'b', 2],
                    ['ILOŚĆ GODZIN:', 1, '', ''],          # February has no totals label
                    ['DANE DO FAKTURY', '', 'notatka', 8])
        months, notes = parse_tab('X', rows, 2026)
        self.assertEqual(notes, [])
        self.assertEqual([e['description'] for e in months['2026-02']], ['b'])

    def test_tab_without_any_totals_row_is_read_to_the_end_with_a_note(self):
        rows = grid(['STYCZEŃ', 'ILOŚĆ GODZIN'], ['a', 1], ['b', 2])
        months, notes = parse_tab('X', rows, 2026)
        self.assertEqual(len(months['2026-01']), 2)
        self.assertEqual(notes, ['brak wiersza sumy (NADGODZINY / ILOŚĆ GODZIN), odczytano wszystkie wiersze'])

    def test_missing_hours_column_is_a_tab_error(self):
        rows = grid(['STYCZEŃ', 'OPIS'], ['NADGODZINY:'])
        with self.assertRaisesRegex(TabError, 'brak kolumny'):
            parse_tab('X', rows, 2026)

    def test_column_letter(self):
        self.assertEqual([column_letter(i) for i in (0, 25, 26, 51)], ['A', 'Z', 'AA', 'AZ'])


class KzpTests(TestCase):
    def test_kzp_vat_only(self):
        for text in ['KZP KOWALSKI 1009.21 + VAT (DO ZAPŁATY JEDYNIE VAT)',
                     'KZP w egzekucji 900 zł + VAT (do zapłaty tylko VAT)']:
            entry = parse_entry({'description': text, 'hours': None, 'expense': 100})
            self.assertEqual(entry.kzp, KZP_VAT_ONLY)
            self.assertIsNone(entry.warning)

    def test_kzp_full(self):
        entry = parse_entry({'description': 'KZP Nowak', 'hours': None, 'expense': '1755'})
        self.assertEqual(entry.kzp, KZP_FULL)

    def test_kzp_without_amount_gives_warning(self):
        entry = parse_entry({'description': 'KZP ABC 638 + VAT', 'hours': None, 'cell': 'L!M5'})
        self.assertIsNone(entry.kzp)
        self.assertEqual(entry.warning, 'KZP bez kwoty w kolumnie KOSZT: „KZP ABC 638 + VAT” (L!M5)')

    def test_kzp_in_the_middle_gives_warning(self):
        entry = parse_entry({'description': 'zwrot KZP ABC', 'hours': None, 'expense': 638})
        self.assertIsNone(entry.kzp)
        self.assertEqual(entry.expense, Decimal('638.00'))
        self.assertIn('Czy to KZP?', entry.warning)

    def test_kzp_lines_and_paid_amount(self):
        client = SimpleNamespace(subscription_net=Decimal('4500'), included_hours=Decimal('18'),
                                 overtime_rate_net=Decimal('300'), overtime_period_months=1,
                                 nip='1', address='a')
        result = calculate(client, Month(2026, 8), [
            {'description': 'KZP KOWALSKI 1009.21 + VAT (DO ZAPŁATY JEDYNIE VAT)', 'expense': 1009.21},
            {'description': 'KZP Nowak', 'expense': 100},
        ])
        names = [line.name for line in result.lines]
        self.assertEqual(names, ['usługa prawna - abonament', 'usługa prawna - koszty zastępstwa',
                                 'usługa prawna - koszty zastępstwa'])
        vat_only, full = result.lines[1], result.lines[2]
        self.assertEqual((vat_only.net, vat_only.vat, vat_only.gross), (Decimal('1009.21'), Decimal('232.12'), Decimal('1241.33')))
        self.assertEqual(vat_only.paid, Decimal('1009.21'))
        self.assertEqual(vat_only.note, KZP_VAT_ONLY)
        self.assertEqual(full.paid, Decimal('0.00'))
        self.assertEqual(result.paid, Decimal('1009.21'))
        self.assertEqual(result.to_pay, result.gross - Decimal('1009.21'))


class SheetSourceTests(FakeSheetMixin, DjangoTestCase):
    """The Google calls are faked: these tests need no network and no key."""

    fixtures = ['test_clients']

    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user('operator', password='x')
        self.client.force_login(user)

    def rows(self, month):
        response = self.client.get(reverse('month', args=[month]))
        self.assertEqual(response.status_code, 200)
        return response, {client.key: result for client, result in response.context['rows']}

    def test_one_bad_tab_does_not_stop_the_others(self):
        self.tabs = {
            'Alfa_2026': grid(['PAŹDZIERNIK', 'ILOŚĆ GODZIN'], ['a', 12], ['NADGODZINY:']),
            'Gamma 2026': grid(['CZYNNOSC', 'ILOŚĆ GODZIN', 'DATA']),     # a different layout
        }
        _, results = self.rows('2026-11')
        self.assertEqual(results['alfa'].overtime_hours, Decimal('7'))
        self.assertIn('Arkusz: zakładka „Gamma 2026”: nie znaleziono wiersza z nazwami miesięcy',
                      results['gamma'].warnings)
        self.assertIn('Arkusz: brak zakładki „Beta_2026” w arkuszu', results['beta'].warnings)

    def test_january_reads_the_tab_of_the_previous_year(self):
        self.tabs = {'Alfa_2026': grid(['GRUDZIEŃ', 'ILOŚĆ GODZIN'], ['a', 6], ['NADGODZINY:'])}
        _, results = self.rows('2027-01')
        self.assertEqual(results['alfa'].hours_total, Decimal('6'))

    def test_whole_sheet_error_shows_a_message(self):
        with mock.patch.object(source, '_fetch_titles', side_effect=SourceError('brak połączenia')):
            response, results = self.rows('2026-11')
            self.assertEqual(results, {})
            self.assertContains(response, 'Nie można odczytać arkusza')
            self.assertContains(response, 'brak połączenia')
            pdf = self.client.get(reverse('report_pdf', args=['2026-11', 'gamma']))
            self.assertEqual(pdf.status_code, 502)


class KeyTests(TestCase):
    @override_settings(GOOGLE_SHEET_ID='test-sheet', GOOGLE_SA_KEY_FILE='', GOOGLE_SA_KEY_JSON='')
    def test_missing_key_is_a_source_error(self):
        with self.assertRaisesRegex(SourceError, 'brak klucza'):
            source._session()

    @override_settings(GOOGLE_SHEET_ID='test-sheet', GOOGLE_SA_KEY_FILE='/nonexistent/key.json')
    def test_unreadable_key_is_a_source_error(self):
        with self.assertRaisesRegex(SourceError, 'nie można wczytać klucza'):
            source._session()
