from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from billing.tests.helpers import FakeSheetMixin, make_tab

ACTIVE = ['alfa', 'beta', 'delta', 'gamma']

# Invented data. Month numbers are the work months of 2026.
TABS = {
    # Subscription 2000 zł with 5 h, 400 zł per extra hour.
    'Alfa_2026': make_tab({
        9: [('umowa wzór', 5), ('spotkanie', 1)],                      # 6 h: 1 h overtime
        10: [('spotkanie', None), ('analiza', 2)],                     # a row with no hours
    }),
    # No subscription, 300 zł per hour.
    'Beta_2026': make_tab({
        10: [('pismo', 4), ('wniosek o uzasadnienie', 1, 100)],        # hours and a cost in one row
    }),
    # Subscription 4500 zł with 18 h, overtime settled every 6 months.
    'Gamma 2026': make_tab({
        1: [('a', 20)], 2: [('a', 17)], 3: [('a', 10)], 4: [('a', 17)], 5: [('a', 16)], 6: [('a', 5)],
        7: [('a', 16)], 8: [('a', 13)], 9: [('a', 22)],
        10: [('a', 21), ('KZP Kowalski 1000 zł + VAT (DO ZAPŁATY JEDYNIE VAT)', None, 1000)],
    }),
    # Subscription 750 zł, rate 0 zł: the hours are never billed. No NIP and no address.
    'Delta_2026': make_tab({10: [('wniosek egzekucja', 1)]}),
}


class ViewTests(FakeSheetMixin, TestCase):
    fixtures = ['test_clients']
    tabs = TABS

    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user('operator', password='x')
        self.client.force_login(user)

    def results(self, month):
        response = self.client.get(reverse('month', args=[month]))
        self.assertEqual(response.status_code, 200)
        return response, {client.key: result for client, result in response.context['rows']}

    def test_login_is_required(self):
        self.client.logout()
        response = self.client.get(reverse('month', args=['2026-10']))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('admin:login'), response['Location'])

    def test_privacy_page_is_public(self):
        self.client.logout()
        response = self.client.get('/privacy')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Polityka prywatności')

    def test_healthz_is_public_and_shows_no_data(self):
        self.client.logout()
        response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'ok\n')

    def test_index_redirects_to_a_month(self):
        response = self.client.get(reverse('index'))
        self.assertEqual(response.status_code, 302)
        self.assertRegex(response['Location'], r'^/\d{4}-\d{2}/$')

    def test_month_selector(self):
        response = self.client.get(reverse('go_to_month'), {'m': '2026-07'})
        self.assertRedirects(response, reverse('month', args=['2026-07']))

    def test_invalid_month_is_404(self):
        self.assertEqual(self.client.get('/2026-13/').status_code, 404)

    def test_october_values(self):
        _, results = self.results('2026-10')
        self.assertEqual(sorted(results), ACTIVE)

        alfa = results['alfa']
        self.assertEqual(alfa.hours_total, Decimal('6'))
        self.assertEqual(alfa.overtime_hours, Decimal('1'))
        self.assertEqual(alfa.net, Decimal('2400.00'))
        self.assertEqual(alfa.warnings, [])

        beta = results['beta']   # no entries in September and no subscription: no lines
        self.assertEqual(beta.lines, [])
        self.assertIn('Brak wpisów za wrzesień 2026', beta.warnings)

    def test_november_values(self):
        _, results = self.results('2026-11')

        alfa = results['alfa']
        self.assertEqual(alfa.hours_total, Decimal('2'))
        self.assertEqual(alfa.net, Decimal('2000.00'))
        # October is column AB (the 10th month, 3 columns each). The warning names the cell.
        self.assertIn('Brak liczby godzin: „spotkanie” (Alfa_2026!AB2)', alfa.warnings)

        beta = results['beta']   # 5 h * 300 + a cost of 100 zł net
        self.assertEqual([(line.name, line.quantity) for line in beta.lines],
                         [('usługa prawna', Decimal('5')), ('usługa prawna - opłata', Decimal('1'))])
        self.assertEqual(beta.net, Decimal('1600.00'))
        self.assertEqual(beta.gross, Decimal('1968.00'))

        gamma = results['gamma']   # July-October = 72 h of 108 h; KZP "tylko VAT" of 1000 zł
        self.assertEqual(gamma.hours_total, Decimal('21'))
        self.assertEqual(gamma.overtime_hours, Decimal('0'))
        self.assertEqual(gamma.period_hours, Decimal('72'))
        self.assertEqual(gamma.period_balance, Decimal('36'))
        self.assertEqual([line.name for line in gamma.lines],
                         ['usługa prawna - abonament', 'usługa prawna - koszty zastępstwa'])
        self.assertEqual(gamma.net, Decimal('5500.00'))
        self.assertEqual(gamma.paid, Decimal('1000.00'))

        delta = results['delta']   # rate 0: only the subscription
        self.assertEqual(delta.hours_total, Decimal('1'))
        self.assertEqual(delta.overtime_hours, Decimal('0'))
        self.assertEqual(delta.net, Decimal('750.00'))
        self.assertEqual(delta.warnings, ['Brak NIP w konfiguracji klienta'])   # no NIP; no address is no warning

    def test_six_month_period_settles_in_july(self):
        response, results = self.results('2026-07')
        gamma = results['gamma']   # January-June: 85 h of 108 h
        self.assertTrue(gamma.settlement)
        self.assertEqual(gamma.period_hours, Decimal('85'))
        self.assertEqual(gamma.overtime_hours, Decimal('0'))
        self.assertContains(response, 'Ta faktura rozlicza nadgodziny całego okresu.')

    def test_inactive_client_is_hidden(self):
        from billing.models import Client
        Client.objects.filter(key='alfa').update(active=False)
        _, results = self.results('2026-10')
        self.assertNotIn('alfa', results)
        self.assertNotIn('epsilon', results)   # inactive in the fixture

    def test_month_view_shows_all_client_details(self):
        response, _ = self.results('2026-10')
        for key in ACTIVE:
            self.assertContains(response, f'id="klient-{key}"')
        self.assertContains(response, 'usługa prawna - abonament</td>', html=False)
        self.assertContains(response, 'usługa prawna - nadgodziny</td>', html=False)
        self.assertContains(response, 'umowa wzór')
        self.assertContains(response, 'id="ostrzezenia"')
        response, _ = self.results('2026-11')
        self.assertContains(response, 'Zapłacono (KZP - tylko VAT)')

    @override_settings(GOOGLE_SHEET_ID='')
    def test_missing_sheet_id_shows_a_message(self):
        response = self.client.get(reverse('month', args=['2026-11']))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Nie można odczytać arkusza')
        self.assertContains(response, 'brak identyfikatora arkusza (GOOGLE_SHEET_ID)')

    def test_unknown_client_is_404(self):
        response = self.client.get(reverse('report_pdf', args=['2026-10', 'nobody']))
        self.assertEqual(response.status_code, 404)

    def test_report_pdf(self):
        response = self.client.get(reverse('report_pdf', args=['2026-10', 'alfa']))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('raport_alfa_2026-09.pdf', response['Content-Disposition'])
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_report_has_only_hours_and_a_total(self):
        from billing.calc import Month
        from billing.models import Client
        from billing.views import _results
        from django.template.loader import render_to_string

        def report(key):
            [(client, result)] = _results([Client.objects.get(key=key)], Month(2026, 11))
            return render_to_string('billing/report_pdf.html', {'client': client, 'result': result})

        beta = report('beta')   # "wniosek o uzasadnienie": 1 h and a cost of 100 zł in one row
        self.assertIn('wniosek o uzasadnienie', beta)
        self.assertNotIn('zł', beta)
        self.assertRegex(beta, r'Razem</td>\s*<td class="hours">5</td>')
        gamma = report('gamma')   # a KZP row is a cost
        self.assertNotIn('KZP', gamma)
        self.assertRegex(gamma, r'Razem</td>\s*<td class="hours">21</td>')
