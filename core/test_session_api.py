import os
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase

from .models import ConsultationSession, ProviderProfile, SessionCharge, User, Wallet
from .session_services import accept_session, assign_provider, create_provider_profile, create_session


@override_settings(RECHARGE_WEBHOOK_SECRET='synthetic-workflow-secret')
class SessionWorkflowAPITests(APITestCase):
    password = 'Workflow-test-only!794'

    def setUp(self):
        self.provider = User.objects.create_user(username='provider', password=self.password, role=User.Role.PROVIDER)
        self.profile = create_provider_profile(self.provider, '10.00')
        self.customer = User.objects.create_user(username='customer', password=self.password)
        self.wallet = Wallet.objects.create(user=self.customer, balance=Decimal('50'))
        self.customer_client = self.authenticated_client(self.customer)
        self.provider_client = self.authenticated_client(self.provider)

    def authenticated_client(self, user):
        client = APIClient()
        token = Token.objects.create(user=user)
        client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        return client

    def action(self, session, action, client=None, data=None):
        return (client or self.customer_client).post(
            f'/api/sessions/{session.pk}/{action}/', {} if data is None else data, format='json'
        )

    def accepted(self):
        session = create_session(self.customer)
        assign_provider(session.pk, self.customer, self.provider)
        return accept_session(session.pk, self.provider)

    def test_full_http_workflow_recharge_to_completed_session(self):
        client = APIClient()
        response = client.post('/api/register/', {'username': 'workflow', 'password': self.password}, format='json')
        self.assertEqual(response.status_code, 201)
        customer = User.objects.get(pk=response.data['id'])
        response = client.post('/api/token/', {'username': 'workflow', 'password': self.password}, format='json')
        self.assertEqual(response.status_code, 200)
        client.credentials(HTTP_AUTHORIZATION=f"Token {response.data['token']}")
        response = client.post('/api/recharges/', {'amount': '50.00'}, format='json')
        self.assertEqual(response.status_code, 201)
        callback = {'reference': response.data['reference'], 'outcome': 'success'}
        webhook_client = APIClient()
        for _ in range(2):
            response = webhook_client.post('/api/recharges/callback/', callback, format='json',
                                          HTTP_X_RECHARGE_WEBHOOK_SECRET='synthetic-workflow-secret')
            self.assertEqual(response.status_code, 200)
        response = webhook_client.post('/api/recharges/callback/', {**callback, 'outcome': 'failed'},
                                       format='json', HTTP_X_RECHARGE_WEBHOOK_SECRET='synthetic-workflow-secret')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(client.get('/api/wallet/').data['balance'], '50.00')
        response = client.post('/api/sessions/', {}, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'unassigned')
        session = ConsultationSession.objects.get(pk=response.data['id'])
        response = self.action(session, 'assign', client, {'provider_id': self.provider.pk})
        self.assertEqual(response.status_code, 200)
        provider_client = APIClient()
        response = provider_client.post('/api/token/', {'username': 'provider', 'password': self.password}, format='json')
        self.assertEqual(response.status_code, 200)
        provider_client.credentials(HTTP_AUTHORIZATION=f"Token {response.data['token']}")
        self.assertEqual(self.action(session, 'accept', provider_client).status_code, 200)
        started_at = timezone.now()
        with patch('core.session_services.timezone.now', return_value=started_at):
            response = self.action(session, 'start', client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['billed_minutes'], 1)
        ProviderProfile.objects.filter(pk=self.profile.pk).update(per_minute_rate=Decimal('99.00'))
        with patch('core.session_services.timezone.now', return_value=started_at + timedelta(seconds=60)):
            for _ in range(2):
                response = self.action(session, 'bill', client)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data['billed_minutes'], 2)
        with patch('core.session_services.timezone.now', return_value=started_at + timedelta(seconds=75)):
            response = self.action(session, 'end', provider_client)
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        customer.wallet.refresh_from_db()
        self.assertEqual(customer.wallet.balance, Decimal('30'))
        self.assertEqual(session.charges.count(), 2)
        self.assertEqual(list(session.charges.order_by('minute_number').values_list('minute_number', flat=True)), [1, 2])
        self.assertFalse(session.charges.exclude(amount=Decimal('10')).exists())
        self.assertEqual(session.billed_minutes, 2)
        self.assertEqual(session.billed_amount, Decimal('20'))
        self.assertEqual(session.rate_snapshot, Decimal('10'))
        self.assertEqual(session.started_at, started_at)
        self.assertEqual(session.ended_at, started_at + timedelta(seconds=75))
        self.assertEqual(session.actual_duration, timedelta(seconds=75))
        self.assertEqual(session.billed_duration, timedelta(minutes=2))
        self.assertEqual(session.status, 'completed')
        self.assertEqual(session.ended_by, 'provider')
        self.assertEqual(session.end_reason, 'participant_ended')
        detail = client.get(f'/api/sessions/{session.pk}/')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data['rate_snapshot'], '10.00')
        self.assertEqual(detail.data['billed_amount'], '20.00')
        self.assertEqual(detail.data['actual_duration_seconds'], 75)
        self.assertEqual(detail.data['actual_duration'], '00:01:15')
        self.assertEqual(detail.data['billed_duration'], '00:02:00')
        self.assertNotIn('billing_checked_at', detail.data)
        self.assertNotIn('password', detail.data)
        self.assertNotIn('wallet_balance', detail.data)

    def test_five_minute_minimum_is_domain_400_and_has_no_partial_start(self):
        session = self.accepted()
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('49.99'))
        response = self.action(session, 'start')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['errors']['code'], 'insufficient_balance')
        session.refresh_from_db()
        self.assertEqual(session.status, 'accepted_by_provider')
        self.assertFalse(session.charges.exists())

    def test_invalid_transition_has_machine_readable_domain_error(self):
        session = create_session(self.customer)
        original_state = ConsultationSession.objects.values().get(pk=session.pk)
        self.wallet.refresh_from_db()
        original_balance = self.wallet.balance
        original_charges = list(SessionCharge.objects.order_by('pk').values())
        for action in ['start', 'bill', 'end']:
            with self.subTest(action=action):
                response = self.action(session, action)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data['errors']['code'], 'invalid_transition')
                self.assertEqual(
                    ConsultationSession.objects.values().get(pk=session.pk), original_state
                )
                self.wallet.refresh_from_db()
                self.assertEqual(self.wallet.balance, original_balance)
                self.assertEqual(list(SessionCharge.objects.order_by('pk').values()), original_charges)

    def test_role_violations_are_forbidden_for_visible_sessions(self):
        session = self.accepted()
        self.assertEqual(self.provider_client.post('/api/sessions/', {}, format='json').status_code, 403)
        for action in ['assign', 'start']:
            response = self.action(session, action, self.provider_client)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.data['errors']['code'], 'forbidden')
        response = self.action(session, 'accept')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data['errors']['code'], 'forbidden')

    def test_nonparticipants_and_missing_ids_have_identical_404_for_every_action(self):
        session = self.accepted()
        outsider = User.objects.create_user(username='outsider')
        outsider_provider = User.objects.create_user(username='outsider-provider', role=User.Role.PROVIDER)
        for user in [outsider, outsider_provider]:
            client = self.authenticated_client(user)
            for suffix in ['', 'assign/', 'accept/', 'start/', 'bill/', 'end/']:
                method = client.get if not suffix else client.post
                real = method(f'/api/sessions/{session.pk}/{suffix}', {}, format='json')
                missing = method('/api/sessions/999999/' + suffix, {}, format='json')
                self.assertEqual(real.status_code, 404)
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(real.data, missing.data)
                self.assertEqual(real.data['errors']['code'], 'not_found')
        session.refresh_from_db()
        self.assertEqual(session.status, 'accepted_by_provider')
        self.assertFalse(SessionCharge.objects.exists())

    def test_all_session_routes_require_authentication(self):
        session = self.accepted()
        requests = [('post', '/api/sessions/'), ('get', f'/api/sessions/{session.pk}/')]
        requests += [('post', f'/api/sessions/{session.pk}/{action}/')
                     for action in ['assign', 'accept', 'start', 'bill', 'end']]
        for method, url in requests:
            response = getattr(self.client, method)(url, {}, format='json')
            self.assertEqual(response.status_code, 401)

    def test_malformed_session_ids_are_controlled_404(self):
        session = self.accepted()
        missing = self.customer_client.get('/api/sessions/999999/').data
        noncanonical = [f'+{session.pk}', f'0{session.pk}', f'{session.pk}_0',
                        ''.join(chr(0x660 + int(c)) for c in str(session.pk))]
        for value in ['abc', '0', '-1', '9' * 100, '1.5', '+13', '1_3', '\uff11\uff13', *noncanonical]:
            for suffix in ['', 'assign/', 'accept/', 'start/', 'bill/', 'end/']:
                method = self.customer_client.get if not suffix else self.customer_client.post
                response = method(f'/api/sessions/{value}/{suffix}', {}, format='json')
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.data['errors']['code'], 'not_found')
                self.assertEqual(response.data, missing)
        self.assertEqual(self.customer_client.get(f'/api/sessions/{session.pk}/').status_code, 200)

    def test_lifecycle_payloads_cannot_inject_clock_or_financial_fields(self):
        session = self.accepted()
        for field in ['now', 'started_at', 'rate_snapshot', 'amount', 'billed_minutes',
                      'billed_amount', 'customer_id', 'ended_by']:
            for action in ['start', 'bill', 'end']:
                response = self.action(session, action, data={field: 'injected'})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data['errors'])
        response = self.customer_client.post('/api/sessions/', {'customer_id': self.customer.pk}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(session.charges.exists())

    def test_assignment_rejects_unknown_or_nonprovider_target_and_extra_fields(self):
        session = create_session(self.customer)
        for payload in [{}, {'provider_id': 999999}, {'provider_id': self.customer.pk},
                        {'provider_id': self.provider.pk, 'rate': '1'}]:
            response = self.action(session, 'assign', data=payload)
            self.assertEqual(response.status_code, 400)
        session.refresh_from_db()
        self.assertEqual(session.status, 'unassigned')

    def test_assignment_survives_created_timestamp_ahead_of_clock(self):
        session = create_session(self.customer)
        now = timezone.now()
        future_created = now + timedelta(milliseconds=5)
        ConsultationSession.objects.filter(pk=session.pk).update(created_at=future_created)
        with patch('core.session_services.timezone.now', return_value=now):
            response = self.action(session, 'assign', data={'provider_id': self.provider.pk})
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, 'assigned')
        self.assertEqual(session.provider, self.profile)
        self.assertEqual(session.created_at, future_created)
        self.assertEqual(session.assigned_at, future_created)
        self.assertIsNone(session.accepted_at)

    def test_acceptance_survives_assigned_timestamp_ahead_of_clock(self):
        session = create_session(self.customer)
        assign_provider(session.pk, self.customer, self.provider)
        now = timezone.now()
        future_assigned = now + timedelta(milliseconds=5)
        ConsultationSession.objects.filter(pk=session.pk).update(assigned_at=future_assigned)
        with patch('core.session_services.timezone.now', return_value=now):
            response = self.action(session, 'accept', self.provider_client)
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        self.assertEqual(session.status, 'accepted_by_provider')
        self.assertEqual(session.assigned_at, future_assigned)
        self.assertEqual(session.accepted_at, future_assigned)
        self.assertLessEqual(session.created_at, session.assigned_at)

    def test_assignment_rejects_fractional_boolean_and_invalid_provider_ids(self):
        session = create_session(self.customer)
        original_state = ConsultationSession.objects.values().get(pk=session.pk)
        for value in [1.5, self.provider.pk + 0.5, True, False, 'not-an-id',
                      f'{self.provider.pk}.5', -self.provider.pk, 0]:
            with self.subTest(provider_id=value):
                response = self.action(session, 'assign', data={'provider_id': value})
                self.assertEqual(response.status_code, 400)
                self.assertIn('provider_id', response.data['errors'])
                self.assertEqual(
                    ConsultationSession.objects.values().get(pk=session.pk), original_state
                )

    def test_assignment_preserves_valid_integer_provider_id_payloads(self):
        for value in [self.provider.pk, str(self.provider.pk), float(self.provider.pk),
                      f'{self.provider.pk}.0']:
            with self.subTest(provider_id=value):
                session = create_session(self.customer)
                response = self.action(session, 'assign', data={'provider_id': value})
                self.assertEqual(response.status_code, 200)
                session.refresh_from_db()
                self.assertEqual(session.status, 'assigned')
                self.assertEqual(session.provider, self.profile)

    def test_bill_trigger_completes_at_insufficient_funds_boundary(self):
        session = self.accepted()
        started_at = timezone.now()
        with patch('core.session_services.timezone.now', return_value=started_at):
            self.assertEqual(self.action(session, 'start').status_code, 200)
        with patch('core.session_services.timezone.now', return_value=started_at + timedelta(seconds=900)):
            response = self.action(session, 'bill', self.provider_client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'completed')
        self.assertEqual(response.data['ended_by'], 'system')
        self.assertEqual(response.data['end_reason'], 'insufficient_balance')
        session.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(session.charges.count(), 5)
        self.assertEqual(session.billed_minutes, 5)
        self.assertEqual(session.billed_amount, Decimal('50'))
        self.assertEqual(session.actual_duration, timedelta(minutes=5))
        self.assertEqual(session.ended_at, started_at + timedelta(minutes=5))
        self.assertEqual(self.wallet.balance, Decimal('0'))
        self.assertEqual(self.action(session, 'bill').status_code, 200)
        response = self.action(session, 'end')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['errors']['code'], 'invalid_transition')

    def test_detail_is_participant_scoped_and_does_not_implicitly_bill(self):
        session = self.accepted()
        self.assertEqual(self.action(session, 'start').status_code, 200)
        session.refresh_from_db()
        with patch('core.session_services.timezone.now', return_value=session.started_at + timedelta(seconds=600)):
            for client in [self.customer_client, self.provider_client]:
                response = client.get(f'/api/sessions/{session.pk}/')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data['billed_minutes'], 1)
        self.assertEqual(session.charges.count(), 1)


@override_settings(DEBUG=True)
class DemoProviderCommandTests(TestCase):
    password = 'Synthetic-demo-only!864'

    def seed(self, **options):
        output = StringIO()
        with patch.dict(os.environ, {'TEST_DEMO_PASSWORD': self.password}):
            call_command('seed_demo', local=True, password_env='TEST_DEMO_PASSWORD',
                         stdout=output, no_color=True, **options)
        return output.getvalue()

    def test_seed_is_idempotent_and_creates_only_provider_and_profile(self):
        User.objects.create_user(username='filler')
        created_output = self.seed()
        provider = User.objects.get(username='demo_provider')
        original_hash = provider.password
        existing_output = self.seed()
        provider.refresh_from_db()
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(ProviderProfile.objects.count(), 1)
        self.assertEqual(provider.role, 'PROVIDER')
        self.assertTrue(provider.check_password(self.password))
        self.assertEqual(provider.password, original_hash)
        self.assertEqual(provider.provider_profile.per_minute_rate, Decimal('10'))
        self.assertFalse(Wallet.objects.exists())
        self.assertNotEqual(provider.pk, provider.provider_profile.pk)
        self.assertEqual(created_output, f'Demo provider created. User ID: {provider.pk}\n')
        self.assertEqual(existing_output, f'Demo provider already configured. User ID: {provider.pk}\n')
        self.assertNotIn(self.password, created_output + existing_output)
        self.assertNotIn(original_hash, created_output + existing_output)

    def test_seed_requires_explicit_local_mode_and_password_environment(self):
        with self.assertRaises(CommandError):
            call_command('seed_demo', stdout=StringIO())
        with patch.dict(os.environ, {'TEST_EMPTY_DEMO_PASSWORD': ''}):
            with self.assertRaises(CommandError):
                call_command('seed_demo', local=True, password_env='TEST_EMPTY_DEMO_PASSWORD', stdout=StringIO())
        self.assertFalse(User.objects.exists())

    def test_seed_refuses_production_settings(self):
        output = StringIO()
        errors = StringIO()
        with override_settings(DEBUG=False), patch.dict(os.environ, {'TEST_DEMO_PASSWORD': self.password}):
            with self.assertRaises(CommandError) as error:
                call_command('seed_demo', local=True, password_env='TEST_DEMO_PASSWORD',
                             stdout=output, stderr=errors, no_color=True)
        self.assertEqual(str(error.exception),
                         'Demo seeding requires --local and DJANGO_DEBUG=True; never use in production.')
        self.assertNotIn(self.password, str(error.exception) + output.getvalue() + errors.getvalue())
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(errors.getvalue(), '')
        self.assertFalse(User.objects.exists())
        self.assertFalse(ProviderProfile.objects.exists())

    def test_seed_refuses_existing_customer_without_modification(self):
        user = User.objects.create_user(username='demo_provider')
        with self.assertRaises(CommandError):
            self.seed()
        user.refresh_from_db()
        self.assertEqual(user.role, 'USER')
        self.assertFalse(ProviderProfile.objects.exists())

    def test_seed_refuses_password_or_rate_conflicts(self):
        self.seed()
        with self.assertRaises(CommandError):
            self.seed(rate='20.00')
        with patch.dict(os.environ, {'TEST_OTHER_DEMO_PASSWORD': 'Another-synthetic-pass!991'}):
            with self.assertRaises(CommandError):
                call_command('seed_demo', local=True, password_env='TEST_OTHER_DEMO_PASSWORD', stdout=StringIO())

    def test_seed_invalid_inputs_leave_no_partial_account(self):
        for options in [{'rate': '0'}, {'rate': '10.005'}, {'rate': 'NaN'}, {'username': 'bad name'}]:
            with self.assertRaises(CommandError):
                self.seed(**options)
        self.assertFalse(User.objects.exists())
