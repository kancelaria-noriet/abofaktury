"""Step 3: Gmail connection and the emails with the invoice and the report. Google and Subiekt are faked."""

import base64
import email
import email.policy
import json
import os
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest import TestCase, mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase as DjangoTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing import gmail, mailing, subiekt
from billing.calc import Month
from billing.models import Client, EmailSend, GmailConnection, SubiektInvoice
from billing.tests.helpers import FakeSheetMixin, make_tab
from billing.tests.test_invoices import FakeSubiekt
from billing.tests.test_subiekt import CredentialsMixin, response, save_connection


def id_token(email_address):
    payload = base64.urlsafe_b64encode(json.dumps({'email': email_address}).encode()).decode().rstrip('=')
    return f'header.{payload}.signature'


class FakeGoogle:
    """A fake token endpoint and Gmail send endpoint that record the calls."""

    def __init__(self):
        self.token_calls, self.sent = [], []

    def __call__(self, url, data=None, json=None, headers=None, timeout=None):
        if url == gmail.TOKEN_URL:
            self.token_calls.append(data)
            if data['grant_type'] == 'authorization_code':
                return response(200, {'access_token': 'g-at', 'refresh_token': 'g-rt', 'expires_in': 3600,
                                      'id_token': id_token('sender@example.com')})
            return response(200, {'access_token': 'g-at2', 'expires_in': 3600})
        if url == gmail.SEND_URL:
            message = email.message_from_bytes(base64.urlsafe_b64decode(json['raw']), policy=email.policy.default)
            self.sent.append(message)
            return response(200, {'id': f'msg-{len(self.sent)}'})
        raise AssertionError(f'unexpected call {url}')


class GmailFileMixin:
    def setUp(self):
        super().setUp()
        handle, self.client_file = tempfile.mkstemp(suffix='.json')
        with os.fdopen(handle, 'w') as f:
            json.dump({'installed': {'client_id': 'g-client', 'client_secret': 'g+secret'}}, f)
        self.addCleanup(os.remove, self.client_file)
        patcher = override_settings(GMAIL_CLIENT_FILE=self.client_file, GMAIL_CLIENT_JSON='',
                                    EMAIL_FROM='Kancelaria <faktury@example.com>', EMAIL_ACCOUNT='sender@example.com',
                                    EMAIL_TEST_INVOICE_NUMBER='FS 9/10/2026',
                                    GMAIL_REDIRECT_URI='https://abofaktury.test/gmail/callback',
                                    ALLOWED_HOSTS=['testserver', 'abofaktury.test', 'other.test'])
        patcher.enable()
        self.addCleanup(patcher.disable)
        self.google = FakeGoogle()
        patch = mock.patch.object(gmail.requests, 'post', side_effect=self.google)
        patch.start()
        self.addCleanup(patch.stop)


def connect_gmail(expires_in=3600):
    now = timezone.now()
    return GmailConnection.objects.create(pk=1, access_token='g-at', refresh_token='g-rt', account='sender@example.com',
                                          expires_at=now + timedelta(seconds=expires_in), connected_at=now)


class MessageTests(TestCase):
    def test_message(self):
        work = Month(2026, 10)
        subject, text = mailing.subject_and_text('FS 12/11/2026', Month(2026, 11), work, None, test=True)
        self.assertEqual(subject, '[TEST] Faktura FS 12/11/2026 – listopad 2026')
        self.assertIn('fakturę nr FS 12/11/2026 oraz raport godzin za październik 2026', text)
        raw = mailing.build_message('Kancelaria <faktury@example.com>', ['a@example.com', 'b@example.com'],
                                    subject, text, [(mailing.invoice_filename('FS 12/11/2026'), b'%PDF-1'),
                                                    (mailing.report_filename(work), b'%PDF-2')])
        message = email.message_from_bytes(raw, policy=email.policy.default)
        self.assertEqual(message['To'], 'a@example.com, b@example.com')
        self.assertEqual(message['Subject'], subject)
        self.assertEqual([part.get_filename() for part in message.iter_attachments()],
                         ['Faktura FS 12-11-2026.pdf', 'Raport godzin - październik 2026.pdf'])


class GmailConnectionTests(GmailFileMixin, DjangoTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_user('operator', password='x'))

    def start(self, host='abofaktury.test'):
        response = self.client.get(reverse('gmail_connect'), HTTP_HOST=host)
        self.assertEqual(response.status_code, 302)
        return response

    def test_connect_and_callback(self):
        response = self.start()
        params = parse_qs(urlsplit(response['Location']).query)
        self.assertTrue(response['Location'].startswith(gmail.AUTHORIZE_URL))
        self.assertEqual(params['redirect_uri'], ['https://abofaktury.test/gmail/callback'])
        self.assertIn('https://www.googleapis.com/auth/gmail.send', params['scope'][0])
        self.assertEqual((params['code_challenge_method'], params['access_type'], params['login_hint']),
                         (['S256'], ['offline'], ['sender@example.com']))
        state = params['state'][0]
        verifier = self.client.session['gmail_oauth']['verifier']
        done = self.client.get(reverse('gmail_callback'), {'state': state, 'code': '4/xyz'},
                               HTTP_HOST='abofaktury.test', follow=True)
        self.assertContains(done, 'Połączono z Gmail jako sender@example.com.')
        sent = self.google.token_calls[0]
        self.assertEqual((sent['code'], sent['code_verifier'], sent['client_secret'], sent['redirect_uri']),
                         ('4/xyz', verifier, 'g+secret', 'https://abofaktury.test/gmail/callback'))
        self.assertEqual(gmail.connection().refresh_token, 'g-rt')

    def test_callback_with_wrong_state_does_not_connect(self):
        self.start()
        done = self.client.get(reverse('gmail_callback'), {'state': 'wrong', 'code': '4/xyz'},
                               HTTP_HOST='abofaktury.test', follow=True)
        self.assertContains(done, 'Nieprawidłowy lub wygasły parametr state')
        self.assertEqual(self.google.token_calls, [])
        self.assertIsNone(gmail.connection())

    def test_callback_without_a_started_connection(self):
        done = self.client.get(reverse('gmail_callback'), {'state': 'x', 'code': '4/xyz'},
                               HTTP_HOST='abofaktury.test', follow=True)
        self.assertContains(done, 'Nieprawidłowy lub wygasły parametr state')
        self.assertIsNone(gmail.connection())

    def test_callback_with_error_from_google(self):
        self.start()
        done = self.client.get(reverse('gmail_callback'), {'error': 'access_denied'},
                               HTTP_HOST='abofaktury.test', follow=True)
        self.assertContains(done, 'Google nie udzielił dostępu: access_denied')

    def test_connect_on_another_host_moves_to_the_redirect_host(self):
        response = self.start(host='other.test')
        self.assertEqual(response['Location'], 'https://abofaktury.test/gmail/connect')

    @override_settings(GMAIL_REDIRECT_URI='')
    def test_missing_redirect_uri(self):
        done = self.client.get(reverse('gmail_connect'), HTTP_HOST='abofaktury.test', follow=True)
        self.assertContains(done, 'brak adresu powrotu (GMAIL_REDIRECT_URI)')

    @override_settings(GMAIL_CLIENT_FILE='', GMAIL_CLIENT_JSON='{"web": {"client_id": "env-id", "client_secret": "s"}}')
    def test_client_from_the_env_var(self):
        self.assertEqual(gmail.client_config()['client_id'], 'env-id')

    @override_settings(GMAIL_CLIENT_FILE='', GMAIL_CLIENT_JSON='')
    def test_no_client(self):
        with self.assertRaisesRegex(gmail.GmailError, 'GMAIL_CLIENT_JSON albo GMAIL_CLIENT_FILE'):
            gmail.client_config()

    def test_refresh(self):
        connect_gmail(expires_in=10)
        self.assertEqual(gmail.access_token(), 'g-at2')
        self.assertEqual(self.google.token_calls[0]['grant_type'], 'refresh_token')
        self.assertEqual(gmail.connection().refresh_token, 'g-rt')   # kept, Google sends no new one

    def test_rejected_refresh_disconnects(self):
        connect_gmail(expires_in=10)
        with mock.patch.object(gmail.requests, 'post', return_value=response(400, {'error': 'invalid_grant'})):
            with self.assertRaises(gmail.NotConnected):
                gmail.access_token()
        self.assertIsNone(gmail.connection())


TABS = {'Gamma 2026': make_tab({10: [('a', 21)]}), 'Alfa_2026': make_tab({10: [('analiza', 2)]}),
        'Beta_2026': make_tab({10: [('pismo', 4)]}), 'Delta_2026': make_tab({10: [('wniosek', 1)]})}


class EmailBatchTests(GmailFileMixin, FakeSheetMixin, CredentialsMixin, DjangoTestCase):
    fixtures = ['test_clients']
    tabs = TABS

    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_user('operator', password='x'))
        Client.objects.filter(key='gamma').update(invoice_emails='ksiegowosc@gamma.example, szef@gamma.example')
        connect_gmail()
        save_connection(expires_in=3600)
        self.api = FakeSubiekt()
        # The test invoice: an invoice of another company, already in KSeF.
        self.api.listed.append(({'id': 'test-doc', 'documentNumber': 'FS 9/10/2026', 'issueDate': '2026-10-22T00:00:00',
                                 'kind': 'Invoice', 'clientTin': '9999999999', 'totalNet': 1.0, 'ksefNumber': 'K'}, []))
        patch = mock.patch.object(subiekt.requests, 'request', side_effect=self.printing(self.api))
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def printing(api):
        def call(method, url, **kwargs):
            if url.endswith('/printing'):
                api.calls.append((method, url.split('/1.1', 1)[1]))
                content = b'%PDF-test-invoice' if '/test-doc/' in url else b'%PDF-real-invoice'
                return SimpleNamespace(status_code=200, content=content, text='', json=lambda: None)
            return api(method, url, **kwargs)
        return call

    def preview(self):
        page = self.client.get(reverse('email_batch', args=['2026-11']))
        self.assertEqual(page.status_code, 200)
        return page, {row.client.key: row for row in page.context['rows']}

    def send(self, keys, **extra):
        return self.client.post(reverse('email_batch_send', args=['2026-11']), {'send': keys, **extra}, follow=True)

    def add_invoice(self, key='gamma', number='FS 5/11/2026'):
        self.api.documents['doc-g'] = {}
        return SubiektInvoice.objects.create(client=Client.objects.get(key=key), invoice_month='2026-11',
                                             document_id='doc-g', document_number=number, net=4500, gross=5535,
                                             created_at=timezone.now())

    @override_settings(EMAIL_TEST_RECIPIENT='tester@example.com')
    def test_test_mode_sends_only_to_the_test_address(self):
        page, rows = self.preview()
        self.assertContains(page, 'Tryb testowy')
        gamma = rows['gamma']
        self.assertEqual(gamma.send_to, ['tester@example.com'])
        self.assertEqual(gamma.recipients, ['ksiegowosc@gamma.example', 'szef@gamma.example'])
        self.assertEqual(gamma.subject, '[TEST] Faktura FS 9/10/2026 – listopad 2026')
        self.send(['alfa', 'beta', 'gamma', 'delta'])
        self.assertEqual(len(self.google.sent), 4)
        for message in self.google.sent:
            self.assertEqual(message['To'], 'tester@example.com')      # never a client address
            self.assertEqual(message['From'], 'Kancelaria <faktury@example.com>')
            invoice, report = list(message.iter_attachments())
            self.assertEqual(invoice.get_content(), b'%PDF-test-invoice')
            self.assertTrue(report.get_content().startswith(b'%PDF'))
        self.assertEqual(EmailSend.objects.filter(test=True).count(), 4)
        self.assertEqual(self.api.count('POST', '/documents/test-doc/printing'), 1)   # once for the batch
        self.assertEqual(self.api.count('POST', '/documents/doc-g/printing'), 0)      # no real invoice PDF

    def test_normal_mode_needs_an_invoice_in_ksef_and_recipients(self):
        self.add_invoice()
        _, rows = self.preview()
        self.assertEqual(rows['alfa'].problem, 'brak faktury utworzonej przez aplikację')
        self.assertEqual(rows['gamma'].problem, 'faktura nie jest jeszcze wysłana do KSeF')
        page = self.send(['gamma'])
        self.assertContains(page, 'nie jest jeszcze wysłana do KSeF')
        self.assertEqual(self.google.sent, [])

    def test_normal_mode_sends_the_real_invoice(self):
        self.add_invoice()
        self.api.ksef['doc-g'] = 'KSEF-1'
        _, rows = self.preview()
        self.assertTrue(rows['gamma'].can_send)
        page = self.send(['gamma'])
        self.assertContains(page, 'wysłano e-mail do ksiegowosc@gamma.example, szef@gamma.example.')
        [message] = self.google.sent
        self.assertEqual(message['To'], 'ksiegowosc@gamma.example, szef@gamma.example')
        self.assertEqual(message['Subject'], 'Faktura FS 5/11/2026 – listopad 2026')
        invoice, _ = list(message.iter_attachments())
        self.assertEqual((invoice.get_filename(), invoice.get_content()), ('Faktura FS 5-11-2026.pdf', b'%PDF-real-invoice'))
        send = EmailSend.objects.get()
        self.assertEqual((send.test, send.invoice_number, send.message_id), (False, 'FS 5/11/2026', 'msg-1'))
        # A second send needs the confirmation.
        page = self.send(['gamma'])
        self.assertContains(page, 'e-mail był już wysłany')
        self.assertEqual(len(self.google.sent), 1)
        self.send(['gamma'], resend_gamma='1')
        self.assertEqual(len(self.google.sent), 2)

    def test_client_without_recipients(self):
        self.add_invoice(key='alfa')
        _, rows = self.preview()
        self.assertEqual(rows['alfa'].problem, 'brak adresu „e-mail do faktur” w konfiguracji klienta')

    def test_setup_problems_stop_everything(self):
        GmailConnection.objects.all().delete()
        page, _ = self.preview()
        self.assertContains(page, 'Gmail nie jest połączony')
        self.assertNotContains(page, 'name="send"')
        page = self.send(['gamma'])
        self.assertContains(page, 'Nie wysłano e-maili: Gmail nie jest połączony')

    @override_settings(EMAIL_TEST_RECIPIENT='tester@example.com', EMAIL_TEST_INVOICE_NUMBER='FS 1/1/2000')
    def test_unknown_test_invoice_stops_everything(self):
        page, _ = self.preview()
        self.assertContains(page, 'tryb testowy: faktura testowa FS 1/1/2000')
        page = self.send(['gamma'])
        self.assertContains(page, 'Nie wysłano e-maili: tryb testowy: faktura testowa FS 1/1/2000')
        self.assertEqual(self.google.sent, [])
