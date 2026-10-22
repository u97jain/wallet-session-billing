from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

from django.db import IntegrityError, close_old_connections, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from .models import RechargeTransaction, User, Wallet
from .services import RechargeError, finalize_recharge, initiate_recharge


class RechargeServiceTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(username='customer')
        self.wallet = Wallet.objects.create(user=self.customer, balance=Decimal('5.00'))

    def assert_balance(self, expected):
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal(expected))

    def test_initiation_creates_pending_without_credit(self):
        recharge = initiate_recharge(self.customer, '10.00')
        self.assertEqual(recharge.customer, self.customer)
        self.assertEqual(recharge.wallet, self.wallet)
        self.assertEqual(recharge.amount, Decimal('10.00'))
        self.assertEqual(recharge.status, RechargeTransaction.Status.PENDING)
        self.assertIsNotNone(recharge.created_at)
        self.assertIsNone(recharge.completed_at)
        self.assertEqual(recharge.reference.version, 4)
        other = initiate_recharge(self.customer, Decimal('1.00'))
        self.assertNotEqual(recharge.reference, other.reference)
        self.assert_balance('5.00')

    def test_success_credits_persisted_amount(self):
        recharge = initiate_recharge(self.customer, '10.25')
        result = finalize_recharge(recharge.reference, 'success')
        self.assertEqual(result.status, 'success')
        self.assertIsNotNone(result.completed_at)
        self.assert_balance('15.25')

    def test_failure_does_not_credit(self):
        recharge = initiate_recharge(self.customer, '10')
        result = finalize_recharge(recharge.reference, 'failed')
        self.assertEqual(result.status, 'failed')
        self.assertIsNotNone(result.completed_at)
        self.assert_balance('5.00')

    def test_duplicate_callbacks_preserve_balance_and_completion_time(self):
        for outcome in ('success', 'failed'):
            with self.subTest(outcome=outcome):
                recharge = initiate_recharge(self.customer, '10')
                first = finalize_recharge(recharge.reference, outcome)
                second = finalize_recharge(recharge.reference, outcome)
                self.assertEqual(second.status, outcome)
                self.assertEqual(second.completed_at, first.completed_at)
        self.assert_balance('15.00')

    def test_conflicting_callbacks_preserve_first_final_state(self):
        for first_outcome, second_outcome in [('success', 'failed'), ('failed', 'success')]:
            with self.subTest(first=first_outcome):
                recharge = initiate_recharge(self.customer, '10')
                first = finalize_recharge(recharge.reference, first_outcome)
                second = finalize_recharge(recharge.reference, second_outcome)
                self.assertEqual(second.status, first_outcome)
                self.assertEqual(second.completed_at, first.completed_at)
        self.assert_balance('15.00')

    def test_invalid_amounts_are_controlled_and_create_nothing(self):
        for amount in ['0', '-1', '10.005', '1.000', '10000000000', '99999999999.99',
                       'NaN', 'Infinity', '-Infinity', 'not-money', '', None, True,
                       10.005, Decimal('sNaN')]:
            with self.subTest(amount=repr(amount)), self.assertRaises(RechargeError):
                initiate_recharge(self.customer, amount)
        self.assertFalse(RechargeTransaction.objects.exists())
        self.assert_balance('5.00')

    def test_provider_cannot_recharge_even_with_wallet(self):
        self.customer.role = User.Role.PROVIDER
        self.customer.save(update_fields=['role'])
        with self.assertRaises(RechargeError) as error:
            initiate_recharge(self.customer, '10')
        self.assertEqual(error.exception.code, 'forbidden')

    def test_stale_customer_role_does_not_bypass_restriction(self):
        User.objects.filter(pk=self.customer.pk).update(role=User.Role.PROVIDER)
        with self.assertRaises(RechargeError):
            initiate_recharge(self.customer, '10')

    def test_missing_wallet_is_controlled(self):
        self.wallet.delete()
        with self.assertRaises(RechargeError) as error:
            initiate_recharge(self.customer, '10')
        self.assertEqual(error.exception.code, 'missing_wallet')

    def test_unsaved_customer_is_controlled(self):
        with self.assertRaises(RechargeError):
            initiate_recharge(User(username='unsaved'), '10')

    def test_invalid_callback_input_does_not_change_pending_transaction(self):
        recharge = initiate_recharge(self.customer, '10')
        for reference, outcome, code in [('bad-reference', 'success', 'invalid_reference'),
                                         (uuid4(), 'success', 'not_found'),
                                         (recharge.reference, 'pending', 'invalid_outcome')]:
            with self.subTest(reference=reference), self.assertRaises(RechargeError) as error:
                finalize_recharge(reference, outcome)
            self.assertEqual(error.exception.code, code)
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, 'pending')
        self.assert_balance('5.00')

    def test_balance_overflow_rolls_back_final_state(self):
        self.wallet.balance = Decimal('9999999999.99')
        self.wallet.save(update_fields=['balance'])
        recharge = initiate_recharge(self.customer, '0.01')
        with self.assertRaises(RechargeError) as error:
            finalize_recharge(recharge.reference, 'success')
        self.assertEqual(error.exception.code, 'balance_limit')
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, 'pending')
        self.assertIsNone(recharge.completed_at)
        self.assert_balance('9999999999.99')

    def test_recharge_history_protects_customer_and_wallet_from_deletion(self):
        initiate_recharge(self.customer, '10')
        with self.assertRaises(ProtectedError):
            self.customer.delete()
        with self.assertRaises(ProtectedError):
            self.wallet.delete()


class RechargeConstraintTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(username='constraint-customer')
        self.wallet = Wallet.objects.create(user=self.customer)

    def create_recharge(self, **values):
        return RechargeTransaction.objects.create(
            customer=self.customer, wallet=self.wallet, **values
        )

    def test_amount_constraint_rejects_zero_and_negative_direct_inserts(self):
        for amount in [Decimal('0'), Decimal('-0.01')]:
            with self.subTest(amount=amount):
                with self.assertRaises(IntegrityError) as error, transaction.atomic():
                    self.create_recharge(amount=amount)
                self.assertEqual(error.exception.__cause__.diag.constraint_name, 'recharge_amount_positive')

    def test_amount_constraint_rejects_direct_update(self):
        recharge = self.create_recharge(amount=Decimal('1'))
        with self.assertRaises(IntegrityError) as error, transaction.atomic():
            RechargeTransaction.objects.filter(pk=recharge.pk).update(amount=0)
        self.assertEqual(error.exception.__cause__.diag.constraint_name, 'recharge_amount_positive')

    def test_status_constraint_rejects_invalid_direct_inserts(self):
        invalid = [('pending', timezone.now()), ('success', None), ('failed', None),
                   ('unknown', None), ('unknown', timezone.now()), ('PENDING', None)]
        for status, completed_at in invalid:
            with self.subTest(status=status, completed_at=completed_at):
                with self.assertRaises(IntegrityError) as error, transaction.atomic():
                    self.create_recharge(amount=Decimal('1'), status=status, completed_at=completed_at)
                self.assertEqual(error.exception.__cause__.diag.constraint_name, 'recharge_status_completion_valid')

    def test_status_constraint_rejects_direct_update(self):
        recharge = self.create_recharge(amount=Decimal('1'))
        with self.assertRaises(IntegrityError) as error, transaction.atomic():
            RechargeTransaction.objects.filter(pk=recharge.pk).update(status='success')
        self.assertEqual(error.exception.__cause__.diag.constraint_name, 'recharge_status_completion_valid')

    def test_status_constraint_accepts_all_valid_combinations(self):
        for status in ['pending', 'success', 'failed']:
            completed_at = None if status == 'pending' else timezone.now()
            recharge = self.create_recharge(amount=Decimal('0.01'), status=status, completed_at=completed_at)
            recharge.refresh_from_db()
            self.assertEqual(recharge.status, status)
            self.assertEqual(recharge.completed_at, completed_at)


class RechargeStatusMigrationTests(TransactionTestCase):
    old_target = [('core', '0003_recharge_transaction')]
    new_target = [('core', '0004_lowercase_recharge_status')]

    def tearDown(self):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        finally:
            super().tearDown()

    def test_forward_and_reverse_preserve_recharge_data(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.old_target)
        apps = executor.loader.project_state(self.old_target).apps
        user = apps.get_model('core', 'User').objects.create(username='migration-customer')
        wallet = apps.get_model('core', 'Wallet').objects.create(user=user, balance=Decimal('7.00'))
        recharge_model = apps.get_model('core', 'RechargeTransaction')
        for status in ['PENDING', 'SUCCESS', 'FAILED']:
            recharge_model.objects.create(
                customer=user, wallet=wallet, amount=Decimal('1.25'), status=status,
                completed_at=None if status == 'PENDING' else timezone.now(),
            )
        fields = ['id', 'reference', 'customer_id', 'wallet_id', 'amount', 'created_at', 'completed_at']
        before = list(recharge_model.objects.order_by('id').values_list(*fields))

        executor = MigrationExecutor(connection)
        executor.migrate(self.new_target)
        apps = executor.loader.project_state(self.new_target).apps
        migrated = apps.get_model('core', 'RechargeTransaction').objects.order_by('id')
        self.assertEqual(list(migrated.values_list('status', flat=True)), ['pending', 'success', 'failed'])
        self.assertEqual(list(migrated.values_list(*fields)), before)
        self.assertEqual(apps.get_model('core', 'Wallet').objects.get(pk=wallet.pk).balance, Decimal('7.00'))

        executor = MigrationExecutor(connection)
        executor.migrate(self.old_target)
        apps = executor.loader.project_state(self.old_target).apps
        restored = apps.get_model('core', 'RechargeTransaction').objects.order_by('id')
        self.assertEqual(list(restored.values_list('status', flat=True)), ['PENDING', 'SUCCESS', 'FAILED'])
        self.assertEqual(list(restored.values_list(*fields)), before)


class ConcurrentRechargeTests(TransactionTestCase):
    def setUp(self):
        self.customer = User.objects.create_user(username='customer')
        self.wallet = Wallet.objects.create(user=self.customer)

    def run_callbacks(self, callbacks):
        self.assertEqual(connection.vendor, 'postgresql')
        barrier = Barrier(len(callbacks))

        def callback(reference, outcome):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                    cursor.execute("SET statement_timeout = '10s'")
                barrier.wait(timeout=10)
                result = finalize_recharge(reference, outcome)
                return result.status, result.completed_at
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(callbacks)) as executor:
            futures = [executor.submit(callback, *args) for args in callbacks]
            return [future.result(timeout=20) for future in futures]

    def test_concurrent_duplicate_success_credits_once(self):
        recharge = initiate_recharge(self.customer, '10.25')
        results = self.run_callbacks([(recharge.reference, 'success')] * 2)
        self.assertEqual(results[0], results[1])
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('10.25'))
        recharge.refresh_from_db()
        self.assertEqual(recharge.status, 'success')

    def test_concurrent_conflicting_callbacks_first_final_state_wins(self):
        recharge = initiate_recharge(self.customer, '10')
        results = self.run_callbacks([(recharge.reference, 'success'),
                                      (recharge.reference, 'failed')])
        self.assertEqual(results[0], results[1])
        recharge.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('10') if recharge.status == 'success' else Decimal('0'))

    def test_concurrent_different_recharges_do_not_lose_credits(self):
        first = initiate_recharge(self.customer, '10')
        second = initiate_recharge(self.customer, '20')
        self.run_callbacks([(first.reference, 'success'), (second.reference, 'success')])
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal('30'))
