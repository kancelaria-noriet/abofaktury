import base64
import hashlib
import os
import tempfile
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from billing import subiekt
from billing.models import SubiektConnection

REDIRECT = 'http://abofaktury.test:8090/subiekt/callback'


def response(status, body):
    content = b'' if body is None else str(body).encode()
    return SimpleNamespace(status_code=status, json=lambda: body, text=str(body), content=content)


class CredentialsMixin:
    def setUp(self):
        super().setUp()
        handle, self.creds_file = tempfile.mkstemp()
        with os.fdopen(handle, 'w') as f:
            f.write('ClientId=client-1\nClientSecret=se+cret=1\nSubscriptionKey=sub-1\n')
        self.addCleanup(os.remove, self.creds_file)
        patcher = override_settings(SUBIEKT_CREDENTIALS_FILE=self.creds_file, SUBIEKT_REDIRECT_URI=REDIRECT,
                                    ALLOWED_HOSTS=['testserver', 'abofaktury.test', 'other.test'])
        patcher.enable()
        self.addCleanup(patcher.disable)


class CredentialsTests(CredentialsMixin, TestCase):
    def test_reads_the_three_values(self):
        self.assertEqual(subiekt.credentials(), {'ClientId': 'client-1', 'ClientSecret': 'se+cret=1',
                                                 'SubscriptionKey': 'sub-1'})

    def test_missing_value(self):
        with open(self.creds_file, 'w') as f:
            f.write('ClientId=client-1\n')
        with self.assertRaisesRegex(subiekt.SubiektError, 'ClientSecret, SubscriptionKey'):
            subiekt.credentials()

    @override_settings(SUBIEKT_CREDENTIALS_FILE='/nonexistent/subiekt-api.env')
    def test_missing_file(self):
        with self.assertRaisesRegex(subiekt.SubiektError, 'nie można wczytać'):
            subiekt.credentials()

    def test_pkce_challenge_is_s256_of_the_verifier(self):
        verifier, challenge = subiekt.new_pkce()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(challenge, expected)
        self.assertTrue(43 <= len(verifier) <= 128)


class ConnectionFlowTests(CredentialsMixin, TestCase):
    def setUp(self):
        super().setUp()
        user = get_user_model().objects.create_user('operator', password='x')
        self.client.force_login(user)

    def connect(self):
        response = self.client.get(reverse('subiekt_connect'), HTTP_HOST='abofaktury.test:8090')
        self.assertEqual(response.status_code, 302)
        return parse_qs(urlsplit(response['Location']).query), response

    def test_connect_redirects_to_insert_with_pkce(self):
        params, response = self.connect()
        self.assertTrue(response['Location'].startswith(subiekt.AUTHORIZE_URL))
        self.assertEqual(params['client_id'], ['client-1'])
        self.assertEqual(params['redirect_uri'], [REDIRECT])
        self.assertEqual(params['code_challenge_method'], ['S256'])
        self.assertIn('subiekt123', params['scope'][0])
        self.assertIn('offline_access', params['scope'][0])
        self.assertEqual(params['state'][0], self.client.session['subiekt_oauth']['state'])

    def test_connect_on_another_host_moves_to_the_redirect_host(self):
        response = self.client.get(reverse('subiekt_connect'), HTTP_HOST='other.test:8090')
        self.assertEqual(response['Location'], 'http://abofaktury.test:8090/subiekt/connect')

    def test_callback_with_wrong_state_does_not_connect(self):
        self.connect()
        response = self.client.get(reverse('subiekt_callback'), {'code': 'c', 'state': 'wrong'},
                                   HTTP_HOST='abofaktury.test:8090', follow=True)
        self.assertContains(response, 'Nieprawidłowy lub wygasły parametr state')
        self.assertIsNone(subiekt.connection())

    def test_callback_stores_the_tokens(self):
        params, _ = self.connect()
        verifier = self.client.session['subiekt_oauth']['verifier']
        tokens = {'access_token': 'at-1', 'refresh_token': 'rt-1', 'expires_in': 3600}
        with mock.patch.object(subiekt.requests, 'post', return_value=response(200, tokens)) as post, \
             mock.patch.object(subiekt.requests, 'get', return_value=response(200, {'email': 'operator@example.com'})):
            page = self.client.get(reverse('subiekt_callback'), {'code': 'code-1', 'state': params['state'][0]},
                                   HTTP_HOST='abofaktury.test:8090', follow=True)
        sent = post.call_args.kwargs
        self.assertEqual(sent['data']['code_verifier'], verifier)
        self.assertEqual(sent['data']['redirect_uri'], REDIRECT)
        self.assertNotIn('auth', sent)   # client_secret_post, not HTTP Basic
        self.assertEqual((sent['data']['client_id'], sent['data']['client_secret']), ('client-1', 'se+cret=1'))
        conn = subiekt.connection()
        self.assertEqual((conn.access_token, conn.refresh_token, conn.account), ('at-1', 'rt-1', 'operator@example.com'))
        self.assertContains(page, 'Połączono z Subiektem jako operator@example.com')

    def test_callback_with_error_from_insert(self):
        self.connect()
        page = self.client.get(reverse('subiekt_callback'), {'error': 'access_denied'},
                               HTTP_HOST='abofaktury.test:8090', follow=True)
        self.assertContains(page, 'InsERT nie udzielił dostępu: access_denied')

    def test_disconnect_deletes_the_tokens(self):
        save_connection(expires_in=3600)
        self.client.post(reverse('subiekt_disconnect'))
        self.assertIsNone(subiekt.connection())

    def test_paging_stops_at_the_total(self):
        save_connection(expires_in=3600)
        full = {'items': [{'id': i, 'tin': ''} for i in range(50)], 'totalItemsCount': 50}
        with mock.patch.object(subiekt.requests, 'request', return_value=response(200, full)) as get:
            self.assertEqual(len(subiekt.list_clients()), 50)
        self.assertEqual(get.call_count, 1)

    def test_client_check(self):
        save_connection(expires_in=3600)
        app_clients = [SimpleNamespace(name='Alfa', nip='123‐456‐78‐90'), SimpleNamespace(name='Delta', nip='')]
        subiekt_clients = [{'id': 7, 'name': 'ALFA SP. Z O.O.', 'tin': 'PL1234567890'}]
        rows = subiekt.match_clients(app_clients, subiekt_clients)
        self.assertEqual(rows[0][1:], ('1234567890', [subiekt_clients[0]]))
        self.assertEqual(rows[1][1:], ('', []))

    def test_client_check_page_reads_all_pages(self):
        save_connection(expires_in=3600)
        page1 = [{'id': i, 'name': f'K{i}', 'tin': str(i)} for i in range(50)]
        page2 = [{'id': 100, 'name': 'GAMMA SP. K.', 'tin': '333-333-33-33'}]
        pages = [response(200, {'items': page1, 'totalItemsCount': 51}),
                 response(200, {'items': page2, 'totalItemsCount': 51})]
        with mock.patch.object(subiekt.requests, 'request', side_effect=pages) as get:
            page = self.client.get(reverse('subiekt'), {'sprawdz': '1'})
        self.assertEqual(get.call_count, 2)
        headers = get.call_args.kwargs['headers']
        self.assertEqual(headers['Authorization'], 'Bearer at-1')
        self.assertEqual(headers['Ocp-Apim-Subscription-Key'], 'sub-1')
        self.assertContains(page, 'Kontrahentów w Subiekcie: 51')
        # The API counts the pages from 0.
        self.assertEqual([c.kwargs['params']['pageNumber'] for c in get.call_args_list], [0, 1])
        self.assertEqual(get.call_args.kwargs['params']['pageSize'], 50)
        self.assertEqual(headers['x-api-version'], '1.1')


def save_connection(expires_in, refresh_token='rt-1'):
    now = timezone.now()
    return SubiektConnection.objects.create(pk=1, access_token='at-1', refresh_token=refresh_token,
                                            expires_at=now + timedelta(seconds=expires_in), connected_at=now)


class TokenRefreshTests(CredentialsMixin, TestCase):
    def test_valid_token_is_used_as_it_is(self):
        save_connection(expires_in=3600)
        with mock.patch.object(subiekt.requests, 'post') as post:
            self.assertEqual(subiekt.access_token(), 'at-1')
        post.assert_not_called()

    def test_expired_token_is_refreshed(self):
        save_connection(expires_in=10)
        tokens = {'access_token': 'at-2', 'refresh_token': 'rt-2', 'expires_in': 3600}
        with mock.patch.object(subiekt.requests, 'post', return_value=response(200, tokens)) as post:
            self.assertEqual(subiekt.access_token(), 'at-2')
        self.assertEqual(post.call_args.kwargs['data'], {'grant_type': 'refresh_token', 'refresh_token': 'rt-1',
                                                         'client_id': 'client-1', 'client_secret': 'se+cret=1'})
        self.assertEqual(subiekt.connection().refresh_token, 'rt-2')

    def test_rejected_refresh_token_disconnects(self):
        save_connection(expires_in=10)
        with mock.patch.object(subiekt.requests, 'post', return_value=response(400, {'error': 'invalid_grant'})):
            with self.assertRaises(subiekt.NotConnected):
                subiekt.access_token()
        self.assertIsNone(subiekt.connection())

    def test_not_connected(self):
        with self.assertRaises(subiekt.NotConnected):
            subiekt.access_token()
