"""Step 2: create the invoice in Subiekt. Invented data; the Subiekt API is faked."""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase, mock

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase, override_settings
from django.urls import reverse

from billing import subiekt
from billing.calc import Month, calculate
from billing.models import SubiektInvoice
from billing.tests.helpers import FakeSheetMixin, make_tab
from billing.tests.test_subiekt import CredentialsMixin, response, save_connection

PRODUCTS = [
    {'id': 'p-abo', 'symbol': '69.10.1 abonament', 'kind': 'Service'},
    {'id': 'p-nad', 'symbol': '69.10.1 nadgodziny', 'kind': 'Service'},
    {'id': 'p-usl', 'symbol': '69.10.1', 'kind': 'Service'},
    {'id': 'p-kzp', 'symbol': '69.10.1 kzp', 'kind': 'Service'},
    {'id': 'p-opl-good', 'symbol': '69.10.1 opłata', 'kind': 'Good'},     # a deactivated copy
    {'id': 'p-opl', 'symbol': '69.10.1 opłata', 'kind': 'Service'},
]
GAMMA = {'id': 'c-gamma', 'name': 'GAMMA SP. K.', 'tin': ' 333-333-33-33', 'kind': 'Company', 'tinKind': 'Nip',
         'address': {'line1': 'ul. Przykładowa 3', 'zipCode': '00-003', 'city': 'Gdańsk', 'country': 'PL'}}
ALL_SYMBOLS = list(subiekt.PRODUCT_SYMBOLS.values())


def result_with_all_lines():
    client = SimpleNamespace(subscription_net=Decimal('4500'), included_hours=Decimal('18'),
                             overtime_rate_net=Decimal('300'), overtime_period_months=1, nip='1', address='a')
    return calculate(client, Month(2026, 11), [
        {'description': 'praca', 'hours': 20},
        {'description': 'opłata sądowa', 'expense': 100},
        {'description': 'KZP Kowalski (do zapłaty tylko VAT)', 'expense': 1000},
    ])


class BuildInvoiceTests(TestCase):
    def test_body(self):
        products = subiekt.find_products(PRODUCTS, ALL_SYMBOLS)
        body = subiekt.build_invoice(result_with_all_lines(), GAMMA, products, 'JAN TESTOWY', date(2026, 11, 18),
                                     'Testowo')
        self.assertEqual({k: body[k] for k in ('kind', 'eInvoice', 'currency', 'issuer', 'calculationMethod',
                                              'paymentMethod', 'splitPayment', 'communityTransaction',
                                              'transactionLocation')},
                         {'kind': 'Invoice', 'eInvoice': True, 'currency': 'PLN', 'issuer': 'JAN TESTOWY',
                          'calculationMethod': 'Net', 'paymentMethod': 'BankTransfer', 'splitPayment': 'False',
                          'communityTransaction': 'SK', 'transactionLocation': 'Testowo'})
        self.assertEqual((body['issueDate'], body['deliveryDate'], body['taxLiabilityDate'], body['dueDate']),
                         ('2026-11-18T00:00:00', '2026-11-18T00:00:00', '2026-11-18T00:00:00', '2026-11-25T00:00:00'))
        self.assertEqual(body['client']['sourceClientId'], 'c-gamma')
        self.assertEqual(body['client']['tin'], ' 333-333-33-33')
        self.assertEqual(
            [(i['no'], i['productName'], i['sourceProductId'], i['quantity'], i['unit'], i['netPrice'], i['vatRate'])
             for i in body['items']],
            [(1, 'usługa prawna - abonament', 'p-abo', 1.0, 'szt', 4500.0, '23%'),
             (2, 'usługa prawna - nadgodziny', 'p-nad', 2.0, 'godzina', 300.0, '23%'),
             (3, 'usługa prawna - opłata', 'p-opl', 1.0, 'szt', 100.0, '23%'),
             (4, 'usługa prawna - koszty zastępstwa', 'p-kzp', 1.0, 'szt', 1000.0, '23%')])
        self.assertEqual(body['payments'], [{'date': '2026-11-18T00:00:00', 'onDocument': True, 'value': 1000.0}])

    def test_empty_payments_without_kzp_vat_only(self):
        client = SimpleNamespace(subscription_net=Decimal('750'), included_hours=Decimal('0'),
                                 overtime_rate_net=Decimal('0'), overtime_period_months=1, nip='1', address='a')
        result = calculate(client, Month(2026, 11), [])
        body = subiekt.build_invoice(result, GAMMA, subiekt.find_products(PRODUCTS, ALL_SYMBOLS), 'X', date(2026, 11, 18))
        # The API requires the field; an empty list means that nothing is paid yet.
        self.assertEqual(body['payments'], [])
        self.assertNotIn('transactionLocation', body)   # no place set

    def test_no_lines_and_no_issuer_are_errors(self):
        client = SimpleNamespace(subscription_net=Decimal('0'), included_hours=Decimal('0'),
                                 overtime_rate_net=Decimal('300'), overtime_period_months=1, nip='1', address='a')
        with self.assertRaisesRegex(subiekt.SubiektError, 'nie ma pozycji'):
            subiekt.build_invoice(calculate(client, Month(2026, 11), []), GAMMA, {}, 'X', date(2026, 11, 18))
        with self.assertRaisesRegex(subiekt.SubiektError, 'SUBIEKT_ISSUER'):
            subiekt.build_invoice(result_with_all_lines(), GAMMA, {}, '', date(2026, 11, 18))

    def test_find_products_takes_the_service(self):
        found = subiekt.find_products(PRODUCTS, ['69.10.1 opłata'])
        self.assertEqual(found['69.10.1 opłata']['id'], 'p-opl')

    def test_find_products_errors(self):
        with self.assertRaisesRegex(subiekt.SubiektError, 'brak produktu .* „69.10.1 xyz”'):
            subiekt.find_products(PRODUCTS, ['69.10.1 xyz'])
        twice = PRODUCTS + [{'id': 'p-kzp2', 'symbol': '69.10.1 kzp', 'kind': 'Service'}]
        with self.assertRaisesRegex(subiekt.SubiektError, 'kilka produktów'):
            subiekt.find_products(twice, ['69.10.1 kzp'])

    def test_find_client(self):
        self.assertEqual(subiekt.find_client([GAMMA], '3333333333')['id'], 'c-gamma')
        with self.assertRaisesRegex(subiekt.SubiektError, 'brak kontrahenta w Subiekcie z NIP 1111111111'):
            subiekt.find_client([GAMMA], '1111111111')
        with self.assertRaisesRegex(subiekt.SubiektError, 'kilku kontrahentów'):
            subiekt.find_client([GAMMA, dict(GAMMA, id='c-2')], '333-333-33-33')
        with self.assertRaisesRegex(subiekt.SubiektError, 'nie ma NIP'):
            subiekt.find_client([GAMMA], '')


class FakeSubiekt:
    """A fake Subiekt 123 API that records the calls."""

    def __init__(self):
        self.calls = []
        self.documents = {}
        self.ksef = {}
        self.totals = {'totalNet': 5500.0, 'totalGross': 6765.0, 'leftToPay': 5765.0}
        self.listed = []    # documents made outside the app: (list item, items)

    def __call__(self, method, url, params=None, json=None, headers=None, timeout=None):
        path = url.split('/1.1', 1)[1]
        self.calls.append((method, path))
        if method == 'GET' and path == '/clients':
            return response(200, {'items': [GAMMA], 'totalItemsCount': 1})
        if method == 'GET' and path == '/products':
            return response(200, {'items': PRODUCTS, 'totalItemsCount': len(PRODUCTS)})
        if method == 'GET' and path == '/documents':
            return response(200, {'items': [item for item, _ in self.listed], 'totalItemsCount': len(self.listed)})
        if method == 'GET' and path.startswith('/documents/') and any(i['id'] == path[11:] for i, _ in self.listed):
            return response(200, {'items': next(lines for i, lines in self.listed if i['id'] == path[11:])})
        if method == 'POST' and path == '/documents':
            self.documents['doc-1'] = json
            return response(200, {'id': {'value': 'doc-1'}})
        document_id = path.rsplit('/', 1)[-1]
        if document_id not in self.documents:
            return response(404, None)
        if method == 'GET':
            return response(200, {'id': document_id, 'documentNumber': 'FS 1/11/2026',
                                  'ksefNumber': self.ksef.get(document_id), **self.totals})
        if method == 'DELETE':
            del self.documents[document_id]
            return response(204, None)
        raise AssertionError(f'unexpected call {method} {path}')

    def count(self, method, path):
        return self.calls.count((method, path))


# Gamma: subscription 4500 zł, overtime every 6 months; October has a KZP "tylko VAT" of 1000 zł.
TABS = {'Gamma 2026': make_tab({10: [('a', 21), ('KZP Kowalski 1000 zł + VAT (DO ZAPŁATY JEDYNIE VAT)', None, 1000)]}),
        'Alfa_2026': make_tab({10: [('analiza', 2)]})}


@override_settings(SUBIEKT_ISSUER='JAN TESTOWY')
class InvoiceViewTests(FakeSheetMixin, CredentialsMixin, DjangoTestCase):
    fixtures = ['test_clients']
    tabs = TABS

    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user('operator', password='x')
        self.client.force_login(user)
        save_connection(expires_in=3600)
        self.api = FakeSubiekt()
        patch = mock.patch.object(subiekt.requests, 'request', side_effect=self.api)
        patch.start()
        self.addCleanup(patch.stop)

    def preview(self, key='gamma'):
        response = self.client.get(reverse('subiekt_invoice', args=['2026-11', key]))
        self.assertEqual(response.status_code, 200)
        return response

    def create(self, signature=None, key='gamma', **extra):
        if signature is None:
            signature = self.preview(key).context['signature']
        return self.client.post(reverse('subiekt_invoice_create', args=['2026-11', key]),
                                {'signature': signature, **extra}, follow=True)

    def add_other_invoice(self, lines, issue_date='2026-11-05T00:00:00', tin='3333333333'):
        item = {'id': f'other-{len(self.api.listed)}', 'documentNumber': f'FS {len(self.api.listed) + 7}/11/2026',
                'issueDate': issue_date, 'kind': 'Invoice', 'clientTin': tin, 'totalNet': 100.0, 'ksefNumber': 'K'}
        self.api.listed.append((item, [{'productName': name} for name in lines]))

    def test_other_invoices_without_subscription_are_only_shown(self):
        self.add_other_invoice(['usługa prawna - konsultacja'])
        self.add_other_invoice(['usługa prawna - abonament'], tin='1111111111')        # another client
        self.add_other_invoice(['usługa prawna - abonament'], issue_date='2026-10-20T00:00:00')   # October
        response = self.preview()
        self.assertContains(response, 'FS 7/11/2026 z 2026-11-05')
        self.assertFalse(response.context['needs_confirmation'])
        self.assertNotContains(response, 'name="confirm_subscription"')
        self.create()
        self.assertEqual(self.api.count('POST', '/documents'), 1)

    def test_other_invoice_with_subscription_needs_confirmation(self):
        self.add_other_invoice(['usługa prawna - abonament'])
        response = self.preview()
        self.assertTrue(response.context['needs_confirmation'])
        self.assertContains(response, 'Klient ma już w tym miesiącu fakturę z abonamentem.')
        page = self.create()
        self.assertContains(page, 'klient ma już w tym miesiącu fakturę z abonamentem')
        self.assertEqual(self.api.count('POST', '/documents'), 0)
        self.create(confirm_subscription='1')
        self.assertEqual(self.api.count('POST', '/documents'), 1)

    def test_month_page_offers_the_button(self):
        response = self.client.get(reverse('month', args=['2026-11']))
        self.assertContains(response, reverse('subiekt_invoice', args=['2026-11', 'gamma']))
        self.assertEqual(self.api.calls, [])   # the month page does not call Subiekt

    def test_preview_creates_nothing(self):
        response = self.preview()
        self.assertContains(response, 'GAMMA SP. K.')
        self.assertContains(response, 'usługa prawna - koszty zastępstwa')
        self.assertContains(response, 'Utwórz fakturę w Subiekcie')
        self.assertEqual(self.api.count('POST', '/documents'), 0)

    def test_create_stores_the_invoice(self):
        page = self.create()
        self.assertEqual(self.api.count('POST', '/documents'), 1)
        body = self.api.documents['doc-1']
        self.assertEqual([i['productName'] for i in body['items']],
                         ['usługa prawna - abonament', 'usługa prawna - koszty zastępstwa'])
        self.assertEqual(body['payments'][0]['value'], 1000.0)
        self.assertEqual(body['issuer'], 'JAN TESTOWY')
        invoice = SubiektInvoice.objects.get(client__key='gamma', invoice_month='2026-11')
        self.assertEqual((invoice.document_id, invoice.document_number), ('doc-1', 'FS 1/11/2026'))
        self.assertEqual((invoice.net, invoice.gross), (Decimal('5500.00'), Decimal('6765.00')))
        self.assertContains(page, 'Gamma Sp. k.: utworzono fakturę FS 1/11/2026 w Subiekcie.')
        self.assertNotContains(page, 'różnią się')
        self.assertContains(page, 'Faktura w Subiekcie: <strong>FS 1/11/2026</strong>', html=False)

    def test_second_create_is_refused(self):
        signature = self.preview().context['signature']
        self.create(signature)
        page = self.create(signature)
        self.assertContains(page, 'już istnieje')
        self.assertEqual(self.api.count('POST', '/documents'), 1)

    def test_changed_data_is_refused(self):
        page = self.create(signature='old')
        self.assertContains(page, 'dane zmieniły się od podglądu')
        self.assertEqual(self.api.count('POST', '/documents'), 0)
        self.assertFalse(SubiektInvoice.objects.exists())

    def test_different_totals_give_a_warning(self):
        self.api.totals = {'totalNet': 5500.0, 'totalGross': 6765.0, 'leftToPay': 6765.0}
        page = self.create()
        self.assertContains(page, 'sumy w Subiekcie różnią się od wyliczenia aplikacji: do zapłaty')

    def test_missing_client_in_subiekt_stops(self):
        response = self.preview('alfa')   # NIP 1111111111 is not in Subiekt
        self.assertContains(response, 'brak kontrahenta w Subiekcie z NIP 1111111111')
        self.assertNotContains(response, 'name="signature"')
        page = self.create(signature='x', key='alfa')
        self.assertContains(page, 'Alfa Sp. z o.o.: nie utworzono faktury: brak kontrahenta')
        self.assertEqual(self.api.count('POST', '/documents'), 0)

    def delete(self):
        return self.client.post(reverse('subiekt_invoice_delete', args=['2026-11', 'gamma']), follow=True)

    def test_delete_before_ksef(self):
        self.create()
        page = self.delete()
        self.assertEqual(self.api.count('DELETE', '/documents/doc-1'), 1)
        self.assertFalse(SubiektInvoice.objects.exists())
        self.assertContains(page, 'Usunięto fakturę FS 1/11/2026 z Subiektu.')

    def test_delete_after_ksef_is_refused(self):
        self.create()
        self.api.ksef['doc-1'] = 'KSEF-123'
        page = self.delete()
        self.assertEqual(self.api.count('DELETE', '/documents/doc-1'), 0)
        self.assertTrue(SubiektInvoice.objects.exists())
        self.assertContains(page, 'ma numer KSeF')

    def test_delete_when_the_document_is_already_gone(self):
        self.create()
        del self.api.documents['doc-1']
        page = self.delete()
        self.assertEqual(self.api.count('DELETE', '/documents/doc-1'), 0)
        self.assertFalse(SubiektInvoice.objects.exists())
        self.assertContains(page, 'nie ma w Subiekcie')


@override_settings(SUBIEKT_ISSUER='JAN TESTOWY')
class BatchCreateTests(FakeSheetMixin, CredentialsMixin, DjangoTestCase):
    """Alfa is not in the fake Subiekt; Gamma is. Beta and Delta have no NIP match either."""

    fixtures = ['test_clients']
    tabs = TABS

    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user('operator', password='x')
        self.client.force_login(user)
        save_connection(expires_in=3600)
        self.api = FakeSubiekt()
        patch = mock.patch.object(subiekt.requests, 'request', side_effect=self.api)
        patch.start()
        self.addCleanup(patch.stop)

    def preview(self):
        response = self.client.get(reverse('subiekt_batch', args=['2026-11']))
        self.assertEqual(response.status_code, 200)
        return response, {draft.client.key: draft for draft in response.context['drafts']}

    def test_preview_loads_the_subiekt_data_once(self):
        _, drafts = self.preview()
        self.assertTrue(drafts['gamma'].can_create)
        self.assertIn('brak kontrahenta', drafts['alfa'].problem)
        self.assertEqual(self.api.count('GET', '/clients'), 1)
        self.assertEqual(self.api.count('GET', '/products'), 1)
        self.assertEqual(self.api.count('GET', '/documents'), 1)
        self.assertEqual(self.api.count('POST', '/documents'), 0)

    def test_batch_creates_the_ticked_invoices(self):
        response, drafts = self.preview()
        self.assertContains(response, 'name="create" value="gamma"')
        self.assertNotContains(response, 'name="create" value="alfa"')    # a problem: no checkbox
        page = self.client.post(reverse('subiekt_batch_create', args=['2026-11']),
                                {'create': ['gamma'], 'sig_gamma': drafts['gamma'].signature}, follow=True)
        self.assertEqual(self.api.count('POST', '/documents'), 1)
        self.assertContains(page, 'Gamma Sp. k.: utworzono fakturę FS 1/11/2026 w Subiekcie.')
        self.assertTrue(SubiektInvoice.objects.filter(client__key='gamma', invoice_month='2026-11').exists())
        # A second batch skips the client that has an invoice now.
        _, drafts = self.preview()
        self.assertFalse(drafts['gamma'].can_create)
        page = self.client.post(reverse('subiekt_batch_create', args=['2026-11']),
                                {'create': ['gamma'], 'sig_gamma': 'x'}, follow=True)
        self.assertContains(page, 'Gamma Sp. k.: faktura już istnieje (FS 1/11/2026).')
        self.assertEqual(self.api.count('POST', '/documents'), 1)

    def test_batch_needs_the_confirmation_for_a_second_subscription(self):
        self.api.listed.append(({'id': 'o1', 'documentNumber': 'FS 7/11/2026', 'issueDate': '2026-11-05T00:00:00',
                                 'kind': 'Invoice', 'clientTin': '3333333333', 'totalNet': 4500.0, 'ksefNumber': 'K'},
                                [{'productName': 'usługa prawna - abonament'}]))
        response, drafts = self.preview()
        self.assertTrue(drafts['gamma'].needs_confirmation)
        self.assertContains(response, 'name="confirm_gamma"')
        data = {'create': ['gamma'], 'sig_gamma': drafts['gamma'].signature}
        page = self.client.post(reverse('subiekt_batch_create', args=['2026-11']), data, follow=True)
        self.assertContains(page, 'fakturę z abonamentem')
        self.assertEqual(self.api.count('POST', '/documents'), 0)
        self.client.post(reverse('subiekt_batch_create', args=['2026-11']), {**data, 'confirm_gamma': '1'})
        self.assertEqual(self.api.count('POST', '/documents'), 1)

    def test_nothing_ticked(self):
        page = self.client.post(reverse('subiekt_batch_create', args=['2026-11']), {}, follow=True)
        self.assertContains(page, 'Nie zaznaczono żadnego klienta.')
