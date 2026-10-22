from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch

from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.urls import resolve
from django.utils import timezone

from .models import ConsultationSession, ProviderProfile, User, Wallet
from .session_services import (
    SessionError,
    start_session,
    accept_session,
    assign_provider,
    create_provider_profile,
    create_session,
    end_session,
)


class SessionFixtures:
    def setUp(self):
        super().setUp()
        self.customer = User.objects.create_user(username='session-customer')
        self.wallet = Wallet.objects.create(user=self.customer, balance=Decimal('50.00'))
        self.provider = User.objects.create_user(username='session-provider', role=User.Role.PROVIDER)
        self.profile = create_provider_profile(self.provider, '10.00')

    def accepted_session(self):
        session = create_session(self.customer)
        assign_provider(session.pk, self.customer, self.provider)
        return accept_session(session.pk, self.provider)


class ConsultationLifecycleTests(SessionFixtures, TestCase):
    def test_profile_requires_provider_role(self):
        with self.assertRaises(SessionError) as error:
            create_provider_profile(self.customer, '10')
        self.assertEqual(error.exception.code, 'forbidden')

    def test_rate_validation_rejects_invalid_values(self):
        other = User.objects.create_user(username='other-provider', role=User.Role.PROVIDER)
        for rate in ['0', '-0.01', '10.005', '10000000000', 'NaN', 'Infinity', '', None, True, 1.2]:
            with self.subTest(rate=rate), self.assertRaises(SessionError) as error:
                create_provider_profile(other, rate)
            self.assertEqual(error.exception.code, 'invalid_rate')
        self.assertFalse(ProviderProfile.objects.filter(user=other).exists())

    def test_duplicate_profile_is_domain_error(self):
        with self.assertRaises(SessionError) as error:
            create_provider_profile(self.provider, '10')
        self.assertEqual(error.exception.code, 'profile_exists')

    def test_create_session_defaults(self):
        session = create_session(self.customer)
        self.assertEqual(session.status, 'unassigned')
        self.assertIsNone(session.provider)
        self.assertIsNone(session.rate_snapshot)
        self.assertIsNone(session.started_at)
        self.assertIsNone(session.ended_at)
        self.assertEqual(session.billed_minutes, 0)
        self.assertEqual(session.billed_amount, Decimal('0'))
        self.assertEqual(session.actual_duration, timedelta(0))

    def test_provider_cannot_create_customer_session(self):
        with self.assertRaises(SessionError) as error:
            create_session(self.provider)
        self.assertEqual(error.exception.code, 'forbidden')

    def test_unsaved_and_inactive_customers_are_rejected(self):
        for customer in [User(username='unsaved'), self.customer]:
            if customer.pk:
                User.objects.filter(pk=customer.pk).update(is_active=False)
            with self.assertRaises(SessionError):
                create_session(customer)

    def test_assignment_and_acceptance(self):
        session = create_session(self.customer)
        session = assign_provider(session.pk, self.customer, self.provider)
        self.assertEqual(session.status, 'assigned')
        self.assertEqual(session.provider, self.profile)
        self.assertIsNotNone(session.assigned_at)
        self.assertIsNone(session.rate_snapshot)
        session = accept_session(session.pk, self.provider)
        self.assertEqual(session.status, 'accepted_by_provider')
        self.assertIsNotNone(session.accepted_at)

    def test_assignment_requires_customer_owner_and_provider_profile(self):
        session = create_session(self.customer)
        stranger = User.objects.create_user(username='stranger')
        with self.assertRaises(SessionError) as error:
            assign_provider(session.pk, stranger, self.provider)
        self.assertEqual(error.exception.code, 'forbidden')
        with self.assertRaises(SessionError):
            assign_provider(session.pk, self.customer, stranger)
        other_provider = User.objects.create_user(username='no-profile', role=User.Role.PROVIDER)
        with self.assertRaises(SessionError) as error:
            assign_provider(session.pk, self.customer, other_provider)
        self.assertEqual(error.exception.code, 'missing_profile')
        session.refresh_from_db()
        self.assertEqual(session.status, 'unassigned')

    def test_assignment_and_acceptance_timestamps_do_not_regress(self):
        for milliseconds in [-5, 5]:
            with self.subTest(milliseconds=milliseconds):
                session = create_session(self.customer)
                assigned_now = session.created_at + timedelta(milliseconds=milliseconds)
                with patch('core.session_services.timezone.now', return_value=assigned_now):
                    assign_provider(session.pk, self.customer, self.provider)
                session.refresh_from_db()
                self.assertEqual(session.status, 'assigned')
                self.assertEqual(session.assigned_at, max(assigned_now, session.created_at))
                accepted_now = session.assigned_at + timedelta(milliseconds=milliseconds)
                with patch('core.session_services.timezone.now', return_value=accepted_now):
                    accept_session(session.pk, self.provider)
                session.refresh_from_db()
                self.assertEqual(session.status, 'accepted_by_provider')
                self.assertEqual(session.accepted_at, max(accepted_now, session.assigned_at))
                self.assertLessEqual(session.created_at, session.assigned_at)
                self.assertLessEqual(session.assigned_at, session.accepted_at)

    def test_only_assigned_provider_can_accept(self):
        session = create_session(self.customer)
        assign_provider(session.pk, self.customer, self.provider)
        other = User.objects.create_user(username='other-provider', role=User.Role.PROVIDER)
        for actor in [other, self.customer]:
            with self.assertRaises(SessionError) as error:
                accept_session(session.pk, actor)
            self.assertEqual(error.exception.code, 'forbidden')

    def test_invalid_transitions_do_not_advance(self):
        session = create_session(self.customer)
        with self.assertRaises(SessionError):
            start_session(session.pk, self.customer)
        with self.assertRaises(SessionError):
            end_session(session.pk, self.customer)
        assign_provider(session.pk, self.customer, self.provider)
        with self.assertRaises(SessionError):
            assign_provider(session.pk, self.customer, self.provider)
        with self.assertRaises(SessionError):
            start_session(session.pk, self.customer)
        accept_session(session.pk, self.provider)
        with self.assertRaises(SessionError):
            accept_session(session.pk, self.provider)
        with self.assertRaises(SessionError):
            end_session(session.pk, self.customer)

    def test_internal_start_threshold_and_first_charge(self):
        session = self.accepted_session()
        session = start_session(session.pk, self.customer)
        self.assertEqual(session.status, 'in_progress')
        self.assertEqual(session.rate_snapshot, Decimal('10.00'))
        self.assertIsNotNone(session.started_at)
        self.assertEqual(session.billed_minutes, 1)
        self.assertEqual(session.billed_amount, Decimal('10.00'))
        self.assertEqual(session.charges.count(), 1)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('40.00'))

    def test_insufficient_balance_preserves_accepted_state(self):
        session = self.accepted_session()
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('49.99'))
        with self.assertRaises(SessionError) as error:
            start_session(session.pk, self.customer)
        self.assertEqual(error.exception.code, 'insufficient_balance')
        session.refresh_from_db()
        self.assertEqual(session.status, 'accepted_by_provider')
        self.assertIsNone(session.rate_snapshot)
        self.assertIsNone(session.started_at)

    def test_missing_wallet_start_is_controlled(self):
        session = self.accepted_session()
        self.wallet.delete()
        with self.assertRaises(SessionError) as error:
            start_session(session.pk, self.customer)
        self.assertEqual(error.exception.code, 'missing_wallet')

    def test_start_requires_customer_owner(self):
        session = self.accepted_session()
        other = User.objects.create_user(username='other')
        for actor in [other, self.provider]:
            with self.assertRaises(SessionError) as error:
                start_session(session.pk, actor)
            self.assertEqual(error.exception.code, 'forbidden')

    def test_nonfinite_wallet_balance_is_controlled(self):
        session = self.accepted_session()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE core_wallet SET balance = 'NaN' WHERE id = %s", [self.wallet.pk])
        with self.assertRaises(SessionError) as error:
            start_session(session.pk, self.customer)
        self.assertEqual(error.exception.code, 'invalid_wallet')

    def test_rate_is_captured_at_start_and_remains_fixed(self):
        session = self.accepted_session()
        ProviderProfile.objects.filter(pk=self.profile.pk).update(per_minute_rate=Decimal('9.00'))
        session = start_session(session.pk, self.customer)
        self.assertEqual(session.rate_snapshot, Decimal('9.00'))
        ProviderProfile.objects.filter(pk=self.profile.pk).update(per_minute_rate=Decimal('20.00'))
        session = end_session(session.pk, self.customer)
        self.assertEqual(session.rate_snapshot, Decimal('9.00'))

    def test_start_rechecks_provider_role_and_price(self):
        session = self.accepted_session()
        User.objects.filter(pk=self.provider.pk).update(role=User.Role.USER)
        with self.assertRaises(SessionError) as error:
            start_session(session.pk, self.customer)
        self.assertEqual(error.exception.code, 'forbidden')
        User.objects.filter(pk=self.provider.pk).update(role=User.Role.PROVIDER)
        with patch('core.session_services._rate', side_effect=SessionError('Invalid price', code='invalid_rate')):
            with self.assertRaises(SessionError):
                start_session(session.pk, self.customer)

    def test_only_one_active_session_and_slot_released_after_end(self):
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('100.00'))
        first, second = self.accepted_session(), self.accepted_session()
        start_session(first.pk, self.customer)
        with self.assertRaises(SessionError) as error:
            start_session(second.pk, self.customer)
        self.assertEqual(error.exception.code, 'already_active')
        end_session(first.pk, self.customer)
        self.assertEqual(start_session(second.pk, self.customer).status, 'in_progress')

    def test_each_participant_can_end_with_actual_duration(self):
        for actor, ended_by in [(self.customer, 'customer'), (self.provider, 'provider')]:
            Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('50.00'))
            session = self.accepted_session()
            session = start_session(session.pk, self.customer)
            ended_at = session.started_at + timedelta(seconds=75, microseconds=250000)
            with patch('core.session_services.timezone.now', return_value=ended_at):
                session = end_session(session.pk, actor)
            self.assertEqual(session.status, 'completed')
            self.assertEqual(session.ended_by, ended_by)
            self.assertEqual(session.end_reason, 'participant_ended')
            self.assertEqual(session.actual_duration_seconds, 75.25)
            self.assertEqual(session.billed_minutes, 2)
            with self.assertRaises(SessionError):
                end_session(session.pk, actor)

    def test_unique_constraint_conflict_is_translated_to_domain_error(self):
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('100.00'))
        first, second = self.accepted_session(), self.accepted_session()
        start_session(first.pk, self.customer)
        with patch('core.session_services.ConsultationSession.objects.filter') as lookup:
            lookup.return_value.exists.return_value = False
            with self.assertRaises(SessionError) as error:
                start_session(second.pk, self.customer)
        self.assertEqual(error.exception.code, 'already_active')
        second.refresh_from_db()
        self.assertEqual(second.status, 'accepted_by_provider')

    def test_nonparticipant_cannot_end(self):
        session = self.accepted_session()
        start_session(session.pk, self.customer)
        outsider = User.objects.create_user(username='outsider')
        with self.assertRaises(SessionError) as error:
            end_session(session.pk, outsider)
        self.assertEqual(error.exception.code, 'forbidden')

    def test_invalid_and_unknown_identifiers_are_controlled(self):
        for session_id, code in [('bad', 'invalid_session'), (False, 'invalid_session'), (999999, 'not_found')]:
            with self.assertRaises(SessionError) as error:
                accept_session(session_id, self.provider)
            self.assertEqual(error.exception.code, code)

    def test_history_protects_accounts_and_provider_profile(self):
        self.accepted_session()
        for obj in [self.customer, self.provider, self.profile]:
            with self.assertRaises(ProtectedError):
                obj.delete()

    def test_session_start_route_uses_charged_start_action(self):
        match = resolve('/api/sessions/1/start/')
        self.assertEqual(match.url_name, 'session-start')
        self.assertEqual(match.func.initkwargs['action'], 'start')

    def test_database_rejects_nonpositive_provider_rate(self):
        with self.assertRaises(IntegrityError) as error, transaction.atomic():
            ProviderProfile.objects.filter(pk=self.profile.pk).update(per_minute_rate=0)
        self.assertEqual(error.exception.__cause__.diag.constraint_name, 'provider_rate_positive')

    def test_database_rejects_invalid_session_fields(self):
        session = create_session(self.customer)
        for values in [{'status': 'unknown'}, {'status': 'assigned'},
                       {'billed_amount': Decimal('-0.01')}, {'billed_minutes': 1},
                       {'actual_duration': timedelta(seconds=-1)}, {'ended_by': 'customer'}]:
            with self.subTest(values=values):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    ConsultationSession.objects.filter(pk=session.pk).update(**values)

    def test_partial_unique_constraint_rejects_bypassed_start(self):
        first, second = self.accepted_session(), self.accepted_session()
        first = start_session(first.pk, self.customer)
        with self.assertRaises(IntegrityError) as error, transaction.atomic():
            ConsultationSession.objects.filter(pk=second.pk).update(
                status='in_progress', started_at=timezone.now(), rate_snapshot=Decimal('10.00')
            )
        self.assertEqual(error.exception.__cause__.diag.constraint_name, 'session_one_in_progress_customer')


class ConcurrentSessionStartTests(SessionFixtures, TransactionTestCase):
    def test_different_sessions_of_one_customer_have_one_start_winner(self):
        self.assertEqual(connection.vendor, 'postgresql')
        sessions = [self.accepted_session(), self.accepted_session()]
        barrier = Barrier(2)

        def start(session_id):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                customer = User.objects.get(pk=self.customer.pk)
                barrier.wait(timeout=10)
                try:
                    return start_session(session_id, customer).status
                except SessionError as error:
                    return error.code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(start, session.pk) for session in sessions]
            results = [future.result(timeout=20) for future in futures]
        self.assertCountEqual(results, ['in_progress', 'already_active'])
        self.assertEqual(ConsultationSession.objects.filter(status='in_progress').count(), 1)
        self.assertEqual(ConsultationSession.objects.filter(status='accepted_by_provider').count(), 1)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('40.00'))
