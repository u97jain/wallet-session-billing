from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from .models import User, Wallet


class UserModelTests(TestCase):
    def test_custom_user_defaults_to_customer_without_implicit_wallet(self):
        self.assertIs(get_user_model(), User)
        user = User.objects.create_user(username='customer', password='test-only')
        user.refresh_from_db()
        self.assertEqual(user.role, User.Role.USER)
        self.assertTrue(user.check_password('test-only'))
        self.assertFalse(Wallet.objects.filter(user=user).exists())

    def test_provider_role_is_persisted_without_implicit_wallet(self):
        user = User.objects.create_user(username='provider', role=User.Role.PROVIDER)
        user.full_clean()
        user.refresh_from_db()
        self.assertEqual(user.role, User.Role.PROVIDER)
        self.assertFalse(Wallet.objects.filter(user=user).exists())

    def test_invalid_role_fails_model_validation(self):
        user = User(username='invalid', role='INVALID')
        with self.assertRaises(ValidationError) as error:
            user.full_clean()
        self.assertIn('role', error.exception.message_dict)

    def test_database_rejects_invalid_role_on_insert(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create(username='invalid', role='INVALID')

    def test_database_rejects_invalid_role_on_bulk_create(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(username='invalid', role='INVALID')])

    def test_database_rejects_invalid_role_on_direct_update(self):
        user = User.objects.create_user(username='customer')
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=user.pk).update(role='INVALID')
        user.refresh_from_db()
        self.assertEqual(user.role, User.Role.USER)


class AuthenticationDefaultsTests(TestCase):
    class ProtectedView(APIView):
        def get(self, request):
            return Response({'user_id': request.user.pk})

    def test_default_permissions_reject_anonymous_request(self):
        request = APIRequestFactory().get('/')
        response = self.ProtectedView.as_view()(request)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response['WWW-Authenticate'], 'Token')

    def test_default_authentication_accepts_valid_token(self):
        user = User.objects.create_user(username='authenticated')
        token = Token.objects.create(user=user)
        request = APIRequestFactory().get('/', HTTP_AUTHORIZATION=f'Token {token.key}')
        response = self.ProtectedView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'user_id': user.pk})


class WalletModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='customer')

    def test_explicit_wallet_creation_defaults_to_zero_and_has_reverse_relation(self):
        wallet = Wallet.objects.create(user=self.user)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal('0.00'))
        self.assertEqual(self.user.wallet, wallet)

    def test_only_one_wallet_per_user(self):
        Wallet.objects.create(user=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Wallet.objects.create(user=self.user)

    def test_positive_balance_retains_two_decimal_precision(self):
        wallet = Wallet.objects.create(user=self.user, balance=Decimal('12.34'))
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal('12.34'))

    def test_database_rejects_negative_balance_on_insert(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Wallet.objects.create(user=self.user, balance=Decimal('-0.01'))

    def test_database_rejects_negative_balance_on_direct_update(self):
        wallet = Wallet.objects.create(user=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Wallet.objects.filter(pk=wallet.pk).update(balance=Decimal('-0.01'))
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal('0.00'))

    def test_deleting_user_deletes_wallet(self):
        wallet = Wallet.objects.create(user=self.user)
        self.user.delete()
        self.assertFalse(Wallet.objects.filter(pk=wallet.pk).exists())
