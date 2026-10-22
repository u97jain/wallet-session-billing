from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from importlib import import_module
from threading import Barrier
from unittest.mock import patch

from django.apps import apps
from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.db.models import Sum
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .models import ConsultationSession, SessionCharge, Wallet
from .session_services import SessionError, start_session, bill_session, end_session
from .services import finalize_recharge, initiate_recharge
from .test_sessions import SessionFixtures


class BillingTests(SessionFixtures, TestCase):
    def start(self):
        session = self.accepted_session()
        return start_session(session.pk, self.customer, now=timezone.now())

    def assert_ledger(self, session, minutes, balance):
        session.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(session.billed_minutes, minutes)
        self.assertEqual(session.charges.count(), minutes)
        self.assertEqual(list(session.charges.order_by('minute_number').values_list('minute_number', flat=True)),
                         list(range(1, minutes + 1)))
        self.assertEqual(session.billed_amount, session.charges.aggregate(total=Sum('amount'))['total'])
        self.assertEqual(session.billed_duration, timedelta(minutes=minutes))
        self.assertEqual(self.wallet.balance, Decimal(balance))

    def test_start_debits_first_minute_at_exact_five_minute_threshold(self):
        session = self.start()
        self.assert_ledger(session, 1, '40')
        charge = session.charges.get()
        self.assertEqual(charge.amount, Decimal('10'))
        self.assertEqual(charge.wallet, self.wallet)
        self.assertEqual(charge.charged_at, session.started_at)

    def test_boundaries_59999_60_119999_120(self):
        session = self.start()
        for microseconds, count, balance in [(59999000, 1, '40'), (60000000, 2, '30'),
                                              (119999000, 2, '30'), (120000000, 3, '20')]:
            with self.subTest(microseconds=microseconds):
                bill_session(session.pk, now=session.started_at + timedelta(microseconds=microseconds))
                self.assert_ledger(session, count, balance)

    def test_two_minute_happy_path(self):
        session = self.start()
        result = bill_session(session.pk, now=session.started_at + timedelta(seconds=60))
        self.assertEqual(result.status, 'in_progress')
        self.assert_ledger(session, 2, '30')

    def test_repeated_and_earlier_billing_never_refunds_or_duplicates(self):
        session = self.start()
        for seconds in [120, 120, 30, -1, 119]:
            bill_session(session.pk, now=session.started_at + timedelta(seconds=seconds))
        self.assert_ledger(session, 3, '20')
        session.refresh_from_db()
        self.assertEqual(session.billing_checked_at, session.started_at + timedelta(seconds=120))

    def test_partial_final_minute_is_fully_charged(self):
        session = self.start()
        result = end_session(session.pk, self.customer, now=session.started_at + timedelta(seconds=75))
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.actual_duration, timedelta(seconds=75))
        self.assert_ledger(session, 2, '30')

    def test_exact_end_boundary_does_not_start_another_minute(self):
        for microseconds, expected in [(0, 1), (60000000, 1), (60000001, 2), (120000000, 2)]:
            Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('50'))
            session = self.start()
            end_session(session.pk, self.provider, now=session.started_at + timedelta(microseconds=microseconds))
            self.assert_ledger(session, expected, str(50 - expected * 10))

    def test_delayed_tick_ends_at_first_unpaid_boundary(self):
        session = self.start()
        result = bill_session(session.pk, now=session.started_at + timedelta(seconds=1000))
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.ended_by, 'system')
        self.assertEqual(result.end_reason, 'insufficient_balance')
        self.assertEqual(result.ended_at, session.started_at + timedelta(seconds=300))
        self.assertEqual(result.actual_duration, timedelta(seconds=300))
        self.assert_ledger(session, 5, '0')

    def test_insufficient_funds_never_partially_debit(self):
        session = self.start()
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('4.99'))
        result = bill_session(session.pk, now=session.started_at + timedelta(seconds=60))
        self.assertEqual(result.ended_at, session.started_at + timedelta(seconds=60))
        self.assert_ledger(session, 1, '4.99')

    def test_end_settles_and_auto_end_takes_precedence_when_unaffordable(self):
        session = self.start()
        result = end_session(session.pk, self.customer, now=session.started_at + timedelta(seconds=1000))
        self.assertEqual(result.ended_by, 'system')
        self.assertEqual(result.actual_duration, timedelta(minutes=5))
        self.assert_ledger(session, 5, '0')

    def test_completed_billing_is_harmless_and_end_requests_are_controlled(self):
        session = self.start()
        end_session(session.pk, self.customer, now=session.started_at + timedelta(seconds=20))
        for actor in [self.customer, self.provider]:
            with self.assertRaises(SessionError) as error:
                end_session(session.pk, actor, now=session.started_at + timedelta(seconds=120))
            self.assertEqual(error.exception.code, 'invalid_transition')
        bill_session(session.pk, now=session.started_at + timedelta(days=1))
        self.assert_ledger(session, 1, '40')

    def test_backdated_end_uses_already_processed_checkpoint(self):
        session = self.start()
        checkpoint = session.started_at + timedelta(seconds=121)
        bill_session(session.pk, now=checkpoint)
        result = end_session(session.pk, self.customer, now=session.started_at + timedelta(seconds=10))
        self.assertEqual(result.ended_at, checkpoint)
        self.assert_ledger(session, 3, '20')

    def test_profile_rate_change_does_not_change_charges(self):
        session = self.start()
        self.profile.per_minute_rate = Decimal('30')
        self.profile.save(update_fields=['per_minute_rate'])
        bill_session(session.pk, now=session.started_at + timedelta(seconds=60))
        self.assert_ledger(session, 2, '30')
        self.assertFalse(session.charges.exclude(amount=Decimal('10')).exists())

    def test_start_charge_failure_rolls_back_transition_and_wallet(self):
        session = self.accepted_session()
        with patch('core.session_services.SessionCharge.objects.create', side_effect=RuntimeError('Test insert failure')):
            with self.assertRaises(RuntimeError):
                start_session(session.pk, self.customer)
        session.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(session.status, 'accepted_by_provider')
        self.assertIsNone(session.started_at)
        self.assertIsNone(session.rate_snapshot)
        self.assertEqual(session.billed_minutes, 0)
        self.assertFalse(session.charges.exists())
        self.assertEqual(self.wallet.balance, Decimal('50'))

    def test_first_debit_failure_cannot_leave_an_active_session(self):
        session = self.accepted_session()
        with patch('core.session_services._charge_minute', return_value=False):
            with self.assertRaises(SessionError):
                start_session(session.pk, self.customer)
        session.refresh_from_db()
        self.assertEqual(session.status, 'accepted_by_provider')
        self.assertFalse(session.charges.exists())

    def test_migration_refuses_to_silently_enable_uncharged_legacy_sessions(self):
        session = self.accepted_session()
        ConsultationSession.objects.filter(pk=session.pk).update(
            status='in_progress', started_at=timezone.now(), rate_snapshot=Decimal('10')
        )
        guard = import_module('core.migrations.0006_session_billing').reject_uncharged_active_sessions
        with connection.schema_editor(atomic=False) as editor:
            with self.assertRaisesRegex(RuntimeError, 'explicit reconciliation'):
                guard(apps, editor)
        self.assertFalse(session.charges.exists())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('50'))

    def test_failed_multi_minute_settlement_rolls_back_all_new_debits(self):
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('10000'))
        session = self.start()
        insert = SessionCharge.objects.bulk_create
        calls = 0

        def fail_second(rows, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('Test second insert failure')
            return insert(rows, **kwargs)

        with patch('core.session_services.SessionCharge.objects.bulk_create', side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                bill_session(session.pk, now=session.started_at + timedelta(minutes=501))
        self.assertEqual(calls, 2)
        self.assert_ledger(session, 1, '9990')
        self.assertEqual(session.billing_checked_at, session.started_at)

    def test_charge_uniqueness_conflict_becomes_domain_error_without_debit(self):
        session = self.start()
        insert = SessionCharge.objects.bulk_create

        def duplicate(rows, **kwargs):
            rows[0].minute_number = 1
            return insert(rows, **kwargs)

        with patch('core.session_services.SessionCharge.objects.bulk_create', side_effect=duplicate):
            with self.assertRaises(SessionError) as error:
                bill_session(session.pk, now=session.started_at + timedelta(seconds=60))
        self.assertEqual(error.exception.code, 'charge_conflict')
        self.assert_ledger(session, 1, '40')

    def test_long_catchup_uses_bounded_batches_and_one_debit_counter_update(self):
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('20000'))
        session = self.start()
        now = session.started_at + timedelta(minutes=1440)
        insert = SessionCharge.objects.bulk_create
        batch_sizes = []

        def record_batch(rows, **kwargs):
            batch_sizes.append(len(rows))
            self.assertFalse(kwargs.get('ignore_conflicts', False))
            return insert(rows, **kwargs)

        with patch('core.session_services.SessionCharge.objects.bulk_create', side_effect=record_batch):
            with CaptureQueriesContext(connection) as captured:
                bill_session(session.pk, now=now)
        queries = [q['sql'] for q in captured]
        self.assertEqual(batch_sizes, [500, 500, 440])
        self.assertEqual(sum(q.startswith('UPDATE "core_wallet"') for q in queries), 1)
        self.assertEqual(sum(q.startswith('UPDATE "core_consultationsession"') for q in queries), 1)
        self.assertEqual(sum(q.startswith('INSERT INTO "core_sessioncharge"') for q in queries), 3)
        self.assertLessEqual(len(queries), 20)
        self.assert_ledger(session, 1441, '5590')
        self.assertFalse(session.charges.filter(minute_number__gt=1).exclude(
            amount=Decimal('10'), wallet=self.wallet, charged_at=now
        ).exists())
        for timestamp in [now, now - timedelta(minutes=1)]:
            with CaptureQueriesContext(connection) as repeated:
                bill_session(session.pk, now=timestamp)
            self.assertFalse(any(q['sql'].startswith('UPDATE "core_wallet"') for q in repeated))
            self.assertFalse(any(q['sql'].startswith('INSERT INTO "core_sessioncharge"') for q in repeated))
        self.assert_ledger(session, 1441, '5590')
        self.assertEqual(session.billing_checked_at, now)

    def test_batched_exhaustion_preserves_partial_balance_and_unpaid_boundary(self):
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('5054.99'))
        session = self.start()
        now = session.started_at + timedelta(minutes=1000)
        with CaptureQueriesContext(connection) as captured:
            result = bill_session(session.pk, now=now)
        self.assertEqual(sum(q['sql'].startswith('UPDATE "core_wallet"') for q in captured), 1)
        self.assertEqual(sum(q['sql'].startswith('UPDATE "core_consultationsession"') for q in captured), 1)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.ended_by, 'system')
        self.assertEqual(result.end_reason, 'insufficient_balance')
        self.assertEqual(result.ended_at, session.started_at + timedelta(minutes=505))
        self.assertEqual(result.actual_duration, timedelta(minutes=505))
        self.assert_ledger(session, 505, '4.99')

    def test_long_exact_boundary_end_updates_counters_once(self):
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('20000'))
        session = self.start()
        with CaptureQueriesContext(connection) as captured:
            result = end_session(session.pk, self.customer,
                                 now=session.started_at + timedelta(minutes=1001))
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.actual_duration, timedelta(minutes=1001))
        self.assertEqual(sum(q['sql'].startswith('UPDATE "core_wallet"') for q in captured), 1)
        self.assertEqual(sum(q['sql'].startswith('UPDATE "core_consultationsession"') for q in captured), 1)
        self.assert_ledger(session, 1001, '9990')

    def test_counter_update_failure_rolls_back_debit_and_bulk_rows(self):
        session = self.start()
        with patch('core.session_services.ConsultationSession.save', side_effect=RuntimeError('Test update failure')):
            with self.assertRaises(RuntimeError):
                bill_session(session.pk, now=session.started_at + timedelta(minutes=2))
        self.assert_ledger(session, 1, '40')
        self.assertEqual(session.billing_checked_at, session.started_at)

    def test_wallet_update_failure_rolls_back_without_inserting_charges(self):
        session = self.start()

        def fail_wallet_update(execute, sql, params, many, context):
            if sql.startswith('UPDATE "core_wallet"'):
                raise RuntimeError('Test wallet update failure')
            return execute(sql, params, many, context)

        with connection.execute_wrapper(fail_wallet_update):
            with self.assertRaises(RuntimeError):
                bill_session(session.pk, now=session.started_at + timedelta(minutes=2))
        self.assert_ledger(session, 1, '40')

    def test_unexpected_zero_row_debit_is_controlled_without_ledger_changes(self):
        session = self.start()
        with patch('django.db.models.query.QuerySet.update', return_value=0):
            with self.assertRaises(SessionError) as error:
                bill_session(session.pk, now=session.started_at + timedelta(minutes=2))
        self.assertEqual(error.exception.code, 'invalid_wallet')
        self.assert_ledger(session, 1, '40')

    def test_unaffordable_next_minute_completes_even_near_amount_storage_limit(self):
        self.profile.per_minute_rate = Decimal('1999999999.99')
        self.profile.save(update_fields=['per_minute_rate'])
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('9999999999.99'))
        session = self.start()
        bill_session(session.pk, now=session.started_at + timedelta(minutes=4))
        self.assert_ledger(session, 5, '0.04')
        result = bill_session(session.pk, now=session.started_at + timedelta(minutes=5))
        self.assert_ledger(session, 5, '0.04')
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.ended_by, 'system')
        self.assertEqual(result.end_reason, 'insufficient_balance')
        self.assertEqual(result.actual_duration, timedelta(minutes=5))

    def test_minute_range_limit_only_applies_to_affordable_charges(self):
        session = self.start()
        limit = 2147483647
        # Controlled counters isolate the range guard without billions of ledger rows.
        ConsultationSession.objects.filter(pk=session.pk).update(billed_minutes=limit)
        now = session.started_at + timedelta(minutes=limit)
        with patch('core.session_services._check_ledger'):
            with self.assertRaises(SessionError) as error:
                bill_session(session.pk, now=now)
        self.assertEqual(error.exception.code, 'billing_limit')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('40'))
        self.assertEqual(session.charges.count(), 1)
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('0'))
        with patch('core.session_services._check_ledger'):
            result = bill_session(session.pk, now=now)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(result.ended_at, now)
        self.assertEqual(result.billed_minutes, limit)
        self.assertEqual(result.end_reason, 'insufficient_balance')
        self.assertEqual(session.charges.count(), 1)

    def test_invalid_timestamp_is_controlled(self):
        session = self.start()
        for timestamp in [datetime(2026, 1, 1), 'bad']:
            with self.assertRaises(SessionError) as error:
                bill_session(session.pk, now=timestamp)
            self.assertEqual(error.exception.code, 'invalid_time')
        self.assert_ledger(session, 1, '40')

    def test_default_clock_is_used(self):
        session = self.start()
        with patch('core.session_services.timezone.now', return_value=session.started_at + timedelta(seconds=60)):
            bill_session(session.pk)
        self.assert_ledger(session, 2, '30')

    def test_invalid_snapshot_rate_is_controlled_without_debit(self):
        session = self.start()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE core_consultationsession SET rate_snapshot = 'NaN' WHERE id = %s", [session.pk])
        with self.assertRaises(SessionError) as error:
            bill_session(session.pk, now=session.started_at + timedelta(seconds=60))
        self.assertEqual(error.exception.code, 'invalid_rate')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('40'))
        self.assertEqual(session.charges.count(), 1)

    def test_missing_or_corrupt_wallet_is_controlled(self):
        session = self.start()
        with connection.cursor() as cursor:
            cursor.execute("UPDATE core_wallet SET balance = 'NaN' WHERE id = %s", [self.wallet.pk])
        with self.assertRaises(SessionError) as error:
            bill_session(session.pk, now=session.started_at + timedelta(seconds=60))
        self.assertEqual(error.exception.code, 'invalid_wallet')

    def test_counter_corruption_fails_closed(self):
        session = self.start()
        ConsultationSession.objects.filter(pk=session.pk).update(billed_minutes=2)
        with self.assertRaises(SessionError) as error:
            bill_session(session.pk, now=session.started_at + timedelta(seconds=60))
        self.assertEqual(error.exception.code, 'billing_inconsistent')
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('40'))

    def test_billed_amount_overflow_rolls_back_catchup(self):
        self.profile.per_minute_rate = Decimal('1999999999.99')
        self.profile.save(update_fields=['per_minute_rate'])
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('9999999999.99'))
        session = self.start()
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('9999999999.99'))
        with self.assertRaises(SessionError) as error:
            bill_session(session.pk, now=session.started_at + timedelta(seconds=300))
        self.assertEqual(error.exception.code, 'billing_limit')
        self.assert_ledger(session, 1, '9999999999.99')

    def test_charge_history_is_protected_and_constraints_apply_to_direct_writes(self):
        session = self.start()
        for obj in [session, self.wallet]:
            with self.assertRaises(ProtectedError):
                obj.delete()
        for values in [{'minute_number': 1, 'amount': Decimal('10')},
                       {'minute_number': 0, 'amount': Decimal('10')},
                       {'minute_number': 2, 'amount': Decimal('0')},
                       {'minute_number': 2, 'amount': Decimal('-0.01')}]:
            with self.assertRaises(IntegrityError), transaction.atomic():
                SessionCharge.objects.create(session=session, wallet=self.wallet,
                                             charged_at=timezone.now(), **values)


class ConcurrentBillingTests(SessionFixtures, TransactionTestCase):
    def run_parallel(self, callbacks):
        self.assertEqual(connection.vendor, 'postgresql')
        barrier = Barrier(len(callbacks))

        def worker(callback):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                barrier.wait(timeout=10)
                try:
                    return callback().status
                except SessionError as error:
                    return error.code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(callbacks)) as executor:
            futures = [executor.submit(worker, callback) for callback in callbacks]
            return [future.result(timeout=20) for future in futures]

    def start(self):
        session = self.accepted_session()
        return start_session(session.pk, self.customer)

    def test_parallel_ticks_charge_each_minute_once(self):
        session = self.start()
        now = session.started_at + timedelta(seconds=120)
        self.assertEqual(self.run_parallel([lambda: bill_session(session.pk, now=now)] * 2),
                         ['in_progress', 'in_progress'])
        session.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(session.billed_minutes, 3)
        self.assertEqual(session.charges.count(), 3)
        self.assertEqual(self.wallet.balance, Decimal('20'))

    def test_parallel_long_catchup_has_one_aggregate_financial_effect(self):
        Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal('20000'))
        session = self.start()
        now = session.started_at + timedelta(minutes=1001)
        results = self.run_parallel([lambda: bill_session(session.pk, now=now)] * 2)
        self.assertEqual(results, ['in_progress', 'in_progress'])
        session.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(session.charges.count(), 1002)
        self.assertEqual(session.billed_minutes, 1002)
        self.assertEqual(session.billed_amount, Decimal('10020'))
        self.assertEqual(self.wallet.balance, Decimal('9980'))

    def test_recharge_vs_billing_serializes_wallet_credit_and_aggregate_debit(self):
        session = self.start()
        recharge = initiate_recharge(self.customer, '20')
        now = session.started_at + timedelta(minutes=5)
        self.run_parallel([lambda: bill_session(session.pk, now=now),
                           lambda: finalize_recharge(recharge.reference, 'success')])
        session.refresh_from_db()
        self.wallet.refresh_from_db()
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, 'success')
        self.assertIn(session.billed_minutes, [5, 6])
        self.assertEqual(session.charges.count(), session.billed_minutes)
        self.assertEqual(session.billed_amount, Decimal('10') * session.billed_minutes)
        self.assertEqual(self.wallet.balance, Decimal('70') - session.billed_amount)
        if session.billed_minutes == 5:
            self.assertEqual(session.status, 'completed')
            self.assertEqual(session.ended_at, now)
            self.assertEqual(session.ended_by, 'system')
        else:
            self.assertEqual(session.status, 'in_progress')

    def test_stop_vs_bill_race_has_one_consistent_settlement(self):
        session = self.start()
        now = session.started_at + timedelta(seconds=75)
        self.run_parallel([lambda: bill_session(session.pk, now=now),
                           lambda: end_session(session.pk, self.customer, now=now)])
        session.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(session.status, 'completed')
        self.assertEqual(session.billed_minutes, 2)
        self.assertEqual(session.charges.count(), 2)
        self.assertEqual(session.billed_amount, Decimal('20'))
        self.assertEqual(session.actual_duration, timedelta(seconds=75))
        self.assertEqual(self.wallet.balance, Decimal('30'))

    def test_parallel_conflicting_participant_ends_do_not_double_charge(self):
        session = self.start()
        now = session.started_at + timedelta(seconds=75)
        results = self.run_parallel([lambda: end_session(session.pk, self.customer, now=now),
                                     lambda: end_session(session.pk, self.provider, now=now)])
        self.assertCountEqual(results, ['completed', 'invalid_transition'])
        session.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(session.billed_minutes, 2)
        self.assertEqual(session.charges.count(), 2)
        self.assertEqual(self.wallet.balance, Decimal('30'))

    def test_exact_boundary_stop_tick_tie_follows_session_lock_order(self):
        session = self.start()
        now = session.started_at + timedelta(seconds=60)
        self.run_parallel([lambda: bill_session(session.pk, now=now),
                           lambda: end_session(session.pk, self.customer, now=now)])
        session.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(session.status, 'completed')
        self.assertIn(session.billed_minutes, [1, 2])
        self.assertEqual(session.charges.count(), session.billed_minutes)
        self.assertEqual(session.billed_amount, Decimal('10') * session.billed_minutes)
        self.assertEqual(self.wallet.balance, Decimal('50') - session.billed_amount)
        self.assertEqual(session.actual_duration, timedelta(seconds=60))
