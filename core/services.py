from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.validators import DecimalValidator
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .models import RechargeTransaction, User, Wallet


def register_customer(username, password):
    try:
        with transaction.atomic():
            customer = User.objects.create_user(
                username=username, password=password, role=User.Role.USER
            )
            Wallet.objects.create(user=customer)
            return customer
    except IntegrityError as error:
        diagnostic = getattr(error.__cause__, 'diag', None)
        if diagnostic and diagnostic.constraint_name == 'core_user_username_key':
            raise ValidationError({'username': ['A user with that username already exists.']}) from None
        raise


class RechargeError(ValidationError):
    """Expected domain failure; recharge API views translate by ``code``.

    ``not_found`` -> 404; ``forbidden`` -> 403; validation/business-rule codes
    (invalid_amount, invalid_customer, missing_wallet, invalid_outcome,
    invalid_reference, balance_limit) -> 400. This is not a DRF APIException;
    views provide local HTTP translation without a global exception handler.
    """


def _validate_amount(amount):
    if isinstance(amount, (bool, float)) or not isinstance(amount, (str, int, Decimal)):
        raise RechargeError('Amount must be a decimal string or Decimal.', code='invalid_amount')
    try:
        amount = Decimal(amount)
        DecimalValidator(max_digits=12, decimal_places=2)(amount)
    except (InvalidOperation, ValidationError, ValueError):
        raise RechargeError('Amount must have at most 12 digits and 2 decimal places.', code='invalid_amount') from None
    if amount <= 0:
        raise RechargeError('Amount must be positive.', code='invalid_amount')
    return amount


@transaction.atomic
def initiate_recharge(customer, amount):
    amount = _validate_amount(amount)
    if not isinstance(customer, User) or customer.pk is None:
        raise RechargeError('A saved customer is required.', code='invalid_customer')
    role = User.objects.filter(pk=customer.pk).values_list('role', flat=True).first()
    if role is None:
        raise RechargeError('Customer not found.', code='invalid_customer')
    if role != User.Role.USER:
        raise RechargeError('Only USER accounts may recharge.', code='forbidden')
    try:
        wallet = Wallet.objects.select_for_update().get(user_id=customer.pk)
    except Wallet.DoesNotExist:
        raise RechargeError('Customer has no wallet.', code='missing_wallet') from None
    return RechargeTransaction.objects.create(customer=customer, wallet=wallet, amount=amount)


@transaction.atomic
def finalize_recharge(reference, outcome):
    if outcome not in (RechargeTransaction.Status.SUCCESS, RechargeTransaction.Status.FAILED):
        raise RechargeError('Outcome must be success or failed.', code='invalid_outcome')
    try:
        reference = UUID(str(reference))
    except (ValueError, TypeError, AttributeError):
        raise RechargeError('Invalid recharge reference.', code='invalid_reference') from None
    try:
        recharge = RechargeTransaction.objects.get(reference=reference)
    except RechargeTransaction.DoesNotExist:
        raise RechargeError('Recharge not found.', code='not_found') from None

    # Lock order: Session (when present) -> Wallet -> child financial records.
    Wallet.objects.select_for_update().get(pk=recharge.wallet_id)
    changed = RechargeTransaction.objects.filter(
        pk=recharge.pk, status=RechargeTransaction.Status.PENDING
    ).update(status=outcome, completed_at=timezone.now())
    if changed and outcome == RechargeTransaction.Status.SUCCESS:
        maximum_balance = Decimal('9999999999.99')
        credited = Wallet.objects.filter(
            pk=recharge.wallet_id, balance__lte=maximum_balance - recharge.amount
        ).update(balance=F('balance') + recharge.amount)
        if not credited:
            raise RechargeError('Wallet balance limit exceeded.', code='balance_limit')
    recharge.refresh_from_db()
    return recharge
