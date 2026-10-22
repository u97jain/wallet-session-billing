from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from .models import RechargeTransaction, User, Wallet
from .services import initiate_recharge, register_customer


class RegistrationAndTokenTests(APITestCase):
    password = 'Local-test-passphrase!729'

    def test_registration_creates_customer_and_zero_wallet_without_password_disclosure(self):
        response = self.client.post('/api/register/', {
            'username': 'customer', 'password': self.password,
        }, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.data), {'id', 'username', 'role'})
        user = User.objects.get(username='customer')
        self.assertEqual(user.role, User.Role.USER)
        self.assertTrue(user.check_password(self.password))
        self.assertEqual(user.wallet.balance, Decimal('0.00'))
        self.assertFalse(Token.objects.filter(user=user).exists())

    def test_registration_rejects_role_and_balance_injection(self):
        for extra in [{'role': 'PROVIDER'}, {'balance': '100'}, {'initial_balance': '100'}]:
            with self.subTest(field=list(extra)):
                response = self.client.post('/api/register/', {
                    'username': 'customer', 'password': self.password, **extra,
                }, format='json')
                self.assertEqual(response.status_code, 400)
                self.assertIn('errors', response.data)
        self.assertFalse(User.objects.exists())
        self.assertFalse(Wallet.objects.exists())

    def test_invalid_registration_fields(self):
        for payload in [{}, {'username': '', 'password': self.password},
                        {'username': 'bad name', 'password': self.password},
                        {'username': 'valid', 'password': 'short'},
                        {'username': 'valid', 'password': 'password'},
                        {'username': 'valid', 'password': ''}]:
            with self.subTest(payload_fields=list(payload)):
                response = self.client.post('/api/register/', payload, format='json')
                self.assertEqual(response.status_code, 400)
                self.assertIn('errors', response.data)
        self.assertFalse(User.objects.exists())

    def test_duplicate_username_is_validation_error(self):
        register_customer('existing', self.password)
        response = self.client.post('/api/register/', {
            'username': 'existing', 'password': self.password,
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('username', response.data['errors'])
        self.assertEqual(Wallet.objects.count(), 1)

    def test_registration_service_handles_uniqueness_race(self):
        register_customer('existing', self.password)
        with self.assertRaises(ValidationError):
            register_customer('existing', self.password)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(Wallet.objects.count(), 1)

    def test_normalized_username_uniqueness_and_length_are_validated(self):
        register_customer('ffi', self.password)
        for username in ['\ufb03', '\ufb03' * 100]:
            response = self.client.post('/api/register/', {
                'username': username, 'password': self.password,
            }, format='json')
            self.assertEqual(response.status_code, 400)
            self.assertIn('username', response.data['errors'])
        self.assertEqual(User.objects.count(), 1)

    def test_registration_rolls_back_user_if_wallet_creation_fails(self):
        with patch('core.services.Wallet.objects.create', side_effect=IntegrityError):
            with self.assertRaises(IntegrityError):
                register_customer('rollback', self.password)
        self.assertFalse(User.objects.filter(username='rollback').exists())

    def test_credentials_obtain_reusable_token_for_valid_roles(self):
        for role in User.Role.values:
            with self.subTest(role=role):
                user = User.objects.create_user(username=role, password=self.password, role=role)
                credentials = {'username': role, 'password': self.password}
                first = self.client.post('/api/token/', credentials, format='json')
                second = self.client.post('/api/token/', credentials, format='json')
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.data, second.data)
                self.assertTrue(Token.objects.filter(user=user, key=first.data['token']).exists())

    def test_invalid_credentials_do_not_disclose_username_existence(self):
        User.objects.create_user(username='existing', password=self.password)
        errors = []
        for username in ['existing', 'unknown']:
            response = self.client.post('/api/token/', {
                'username': username, 'password': 'incorrect-test-password',
            }, format='json')
            self.assertEqual(response.status_code, 400)
            errors.append(response.data)
        self.assertEqual(errors[0], errors[1])
        self.assertIn('errors', errors[0])

    def test_inactive_user_cannot_obtain_token(self):
        User.objects.create_user(username='inactive', password=self.password, is_active=False)
        response = self.client.post('/api/token/', {
            'username': 'inactive', 'password': self.password,
        }, format='json')
        self.assertEqual(response.status_code, 400)


class CustomerWalletAPITests(APITestCase):
    def setUp(self):
        self.customer = User.objects.create_user(username='customer')
        self.wallet = Wallet.objects.create(user=self.customer, balance=Decimal('5.00'))
        self.token = Token.objects.create(user=self.customer)
        self.client.credentials(HTTP_AUTHORIZATION=f'Token {self.token.key}')

    def test_wallet_returns_only_callers_balance(self):
        other = User.objects.create_user(username='other')
        Wallet.objects.create(user=other, balance=Decimal('99.00'))
        response = self.client.get('/api/wallet/', {'user_id': other.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'balance': '5.00'})

    def test_customer_endpoints_require_token(self):
        for authorization in ['', 'Token invalid-test-token']:
            self.client.credentials(HTTP_AUTHORIZATION=authorization)
            for method, url, data in [('get', '/api/wallet/', {}),
                                      ('post', '/api/recharges/', {'amount': '10'})]:
                with self.subTest(authorization=bool(authorization), url=url):
                    response = getattr(self.client, method)(url, data, format='json')
                    self.assertEqual(response.status_code, 401)
                    self.assertIn('errors', response.data)

    def test_provider_is_forbidden_on_both_customer_endpoints(self):
        self.customer.role = User.Role.PROVIDER
        self.customer.save(update_fields=['role'])
        self.assertEqual(self.client.get('/api/wallet/').status_code, 403)
        response = self.client.post('/api/recharges/', {'amount': '10'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(RechargeTransaction.objects.exists())

    def test_missing_wallet_returns_controlled_errors(self):
        self.wallet.delete()
        for response in [self.client.get('/api/wallet/'),
                         self.client.post('/api/recharges/', {'amount': '10'}, format='json')]:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data['errors']['code'], 'missing_wallet')

    def test_initiation_returns_reference_without_credit(self):
        response = self.client.post('/api/recharges/', {'amount': '10.25'}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(set(response.data), {'reference', 'status'})
        self.assertEqual(response.data['status'], 'pending')
        recharge = RechargeTransaction.objects.get(reference=response.data['reference'])
        self.assertEqual(recharge.customer, self.customer)
        self.assertEqual(recharge.amount, Decimal('10.25'))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('5.00'))

    def test_invalid_recharge_amounts_do_not_persist(self):
        for amount in ['0', '-1', '10.005', '10000000000', 'NaN', 'Infinity', None, True, 'bad']:
            with self.subTest(amount=amount):
                response = self.client.post('/api/recharges/', {'amount': amount}, format='json')
                self.assertEqual(response.status_code, 400)
                self.assertIn('errors', response.data)
        self.assertFalse(RechargeTransaction.objects.exists())

    def test_caller_cannot_choose_recharge_customer_reference_or_status(self):
        for extra in [{'customer': self.customer.pk}, {'reference': str(uuid4())}, {'status': 'success'}]:
            response = self.client.post('/api/recharges/', {'amount': '10', **extra}, format='json')
            self.assertEqual(response.status_code, 400)
        self.assertFalse(RechargeTransaction.objects.exists())


@override_settings(RECHARGE_WEBHOOK_SECRET='synthetic-webhook-test-only')
class RechargeCallbackAPITests(APITestCase):
    def setUp(self):
        self.customer = User.objects.create_user(username='callback-customer')
        self.wallet = Wallet.objects.create(user=self.customer)
        self.recharge = initiate_recharge(self.customer, '10.25')

    def callback(self, requested_outcome, **extra):
        return self.client.post('/api/recharges/callback/', {
            'reference': str(self.recharge.reference), 'outcome': requested_outcome, **extra,
        }, format='json', HTTP_X_RECHARGE_WEBHOOK_SECRET='synthetic-webhook-test-only')

    def assert_financial_state(self, status, balance):
        self.recharge.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.recharge.status, status)
        self.assertEqual(self.wallet.balance, Decimal(balance))

    def test_missing_wrong_or_token_only_authorization_is_denied(self):
        token = Token.objects.create(user=self.customer)
        for headers in [{}, {'HTTP_X_RECHARGE_WEBHOOK_SECRET': 'wrong-test-secret'},
                        {'HTTP_AUTHORIZATION': f'Token {token.key}'}]:
            response = self.client.post('/api/recharges/callback/', {
                'reference': str(self.recharge.reference), 'outcome': 'success',
            }, format='json', **headers)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.data, {'errors': {'detail': 'Invalid webhook authorization.'}})
        self.assert_financial_state('pending', '0')

    def test_missing_webhook_configuration_fails_closed(self):
        with override_settings(RECHARGE_WEBHOOK_SECRET=''):
            response = self.callback('success')
        self.assertEqual(response.status_code, 403)
        self.assert_financial_state('pending', '0')

    def test_valid_secret_succeeds_without_user_token(self):
        response = self.callback('success')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'success')
        self.assert_financial_state('success', '10.25')

    def test_duplicate_success_credits_once_and_preserves_timestamp(self):
        self.assertEqual(self.callback('success').status_code, 200)
        self.recharge.refresh_from_db()
        completed = self.recharge.completed_at
        self.assertEqual(self.callback('success').status_code, 200)
        self.assert_financial_state('success', '10.25')
        self.assertEqual(self.recharge.completed_at, completed)

    def test_failed_and_duplicate_failed_callback_do_not_credit(self):
        self.assertEqual(self.callback('failed').status_code, 200)
        self.assertEqual(self.callback('failed').status_code, 200)
        self.assert_financial_state('failed', '0')

    def test_conflicting_callbacks_return_409_in_both_directions(self):
        for first, second in [('success', 'failed'), ('failed', 'success')]:
            with self.subTest(first=first):
                self.recharge = initiate_recharge(self.customer, '10.25')
                self.assertEqual(self.callback(first).status_code, 200)
                self.recharge.refresh_from_db()
                completed = self.recharge.completed_at
                response = self.callback(second)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data['errors']['code'], 'conflicting_outcome')
                self.assert_financial_state(first, '10.25')
                self.assertEqual(self.recharge.completed_at, completed)

    def test_unknown_reference_returns_404(self):
        response = self.callback('success', reference=str(uuid4()))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['errors']['code'], 'not_found')
        self.assert_financial_state('pending', '0')

    def test_invalid_callback_payloads_return_400_without_effect(self):
        for payload in [{'reference': 'bad'}, {'outcome': 'pending'}, {'outcome': 'SUCCESS'},
                        {'amount': '999.00'}, {'customer': self.customer.pk}]:
            response = self.callback('success', **payload)
            self.assertEqual(response.status_code, 400)
            self.assertIn('errors', response.data)
        self.assert_financial_state('pending', '0')

    def test_overflow_is_controlled_and_preserves_pending_state(self):
        self.wallet.balance = Decimal('9999999999.99')
        self.wallet.save(update_fields=['balance'])
        response = self.callback('success')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['errors']['code'], 'balance_limit')
        self.assert_financial_state('pending', '9999999999.99')
