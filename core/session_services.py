from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import DecimalValidator
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Max, Min, Sum
from django.utils import timezone

from .models import ConsultationSession, ProviderProfile, SessionCharge, User, Wallet


class SessionError(ValidationError):
    """Controlled lifecycle failure: forbidden, not_found, or business validation codes."""


def _account(actor, role=None):
    if not isinstance(actor, User) or actor.pk is None:
        raise SessionError('A saved account is required.', code='invalid_account')
    try:
        account = User.objects.get(pk=actor.pk, is_active=True)
    except User.DoesNotExist:
        raise SessionError('Active account not found.', code='invalid_account') from None
    if role is not None and account.role != role:
        raise SessionError('Account role is not permitted.', code='forbidden')
    return account


def _rate(value):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise SessionError('Rate must be a decimal value.', code='invalid_rate')
    try:
        value = Decimal(value)
        DecimalValidator(max_digits=12, decimal_places=2)(value)
    except (InvalidOperation, ValidationError, ValueError):
        raise SessionError('Rate requires at most 12 digits and 2 decimal places.', code='invalid_rate') from None
    if value <= 0:
        raise SessionError('Rate must be positive.', code='invalid_rate')
    return value


@transaction.atomic
def create_provider_profile(provider, per_minute_rate):
    provider = _account(provider, User.Role.PROVIDER)
    rate = _rate(per_minute_rate)
    try:
        with transaction.atomic():
            return ProviderProfile.objects.create(user=provider, per_minute_rate=rate)
    except IntegrityError as error:
        diagnostic = getattr(error.__cause__, 'diag', None)
        if diagnostic and diagnostic.constraint_name == 'core_providerprofile_user_id_key':
            raise SessionError('Provider profile already exists.', code='profile_exists') from None
        raise


@transaction.atomic
def create_session(customer):
    return ConsultationSession.objects.create(customer=_account(customer, User.Role.USER))


def _locked_session(session_id):
    if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id <= 0:
        raise SessionError('Invalid session identifier.', code='invalid_session')
    try:
        return ConsultationSession.objects.select_for_update().get(pk=session_id)
    except ConsultationSession.DoesNotExist:
        raise SessionError('Session not found.', code='not_found') from None


def _customer_owner(session, customer):
    customer = _account(customer, User.Role.USER)
    if session.customer_id != customer.pk:
        raise SessionError('Only the session customer may perform this action.', code='forbidden')


def _state(session, expected):
    if session.status != expected:
        raise SessionError('Session is not in the required state.', code='invalid_transition')


def _provider_profile(provider):
    provider = _account(provider, User.Role.PROVIDER)
    try:
        profile = ProviderProfile.objects.get(user=provider)
    except ProviderProfile.DoesNotExist:
        raise SessionError('Provider profile is required.', code='missing_profile') from None
    _rate(profile.per_minute_rate)
    return profile


@transaction.atomic
def assign_provider(session_id, customer, provider):
    session = _locked_session(session_id)
    _customer_owner(session, customer)
    _state(session, ConsultationSession.Status.UNASSIGNED)
    session.provider = _provider_profile(provider)
    session.status = ConsultationSession.Status.ASSIGNED
    session.assigned_at = max(timezone.now(), session.created_at)
    session.save(update_fields=['provider', 'status', 'assigned_at'])
    return session


@transaction.atomic
def accept_session(session_id, provider):
    session = _locked_session(session_id)
    provider = _account(provider, User.Role.PROVIDER)
    if session.provider_id is None or session.provider.user_id != provider.pk:
        raise SessionError('Only the assigned provider may accept.', code='forbidden')
    _state(session, ConsultationSession.Status.ASSIGNED)
    _rate(session.provider.per_minute_rate)
    session.status = ConsultationSession.Status.ACCEPTED_BY_PROVIDER
    session.accepted_at = max(timezone.now(), session.assigned_at)
    session.save(update_fields=['status', 'accepted_at'])
    return session


@transaction.atomic
def start_session(session_id, customer, now=None):
    """Atomically start a session with its first advance charge for the start API."""
    session = _locked_session(session_id)
    _customer_owner(session, customer)
    _state(session, ConsultationSession.Status.ACCEPTED_BY_PROVIDER)
    try:
        wallet = Wallet.objects.select_for_update().get(user_id=session.customer_id)
    except Wallet.DoesNotExist:
        raise SessionError('Customer has no wallet.', code='missing_wallet') from None
    # One wallet per customer serializes starts even when different sessions are locked.
    if ConsultationSession.objects.filter(
        customer_id=session.customer_id, status=ConsultationSession.Status.IN_PROGRESS
    ).exists():
        raise SessionError('Customer already has an active session.', code='already_active')
    profile = _provider_profile(session.provider.user)
    rate = _rate(profile.per_minute_rate)
    if not wallet.balance.is_finite() or wallet.balance < 0:
        raise SessionError('Wallet balance is invalid.', code='invalid_wallet')
    if wallet.balance < 5 * rate:
        raise SessionError('At least five minutes of balance is required.', code='insufficient_balance')
    session.rate_snapshot = rate
    session.status = ConsultationSession.Status.IN_PROGRESS
    session.started_at = _timestamp(now)
    if session.started_at < session.accepted_at:
        raise SessionError('Start precedes acceptance.', code='invalid_time')
    session.billing_checked_at = session.started_at
    try:
        with transaction.atomic():
            session.save(update_fields=['rate_snapshot', 'status', 'started_at', 'billing_checked_at'])
            if not _charge_minute(session, wallet, session.started_at):
                raise SessionError('First-minute debit failed.', code='insufficient_balance')
    except IntegrityError as error:
        diagnostic = getattr(error.__cause__, 'diag', None)
        if diagnostic and diagnostic.constraint_name == 'session_one_in_progress_customer':
            raise SessionError('Customer already has an active session.', code='already_active') from None
        raise
    return session


@transaction.atomic
def end_session(session_id, actor, now=None):
    session = _locked_session(session_id)
    actor = _account(actor)
    if actor.pk == session.customer_id and actor.role == User.Role.USER:
        ended_by = ConsultationSession.EndedBy.CUSTOMER
    elif session.provider_id and actor.pk == session.provider.user_id and actor.role == User.Role.PROVIDER:
        ended_by = ConsultationSession.EndedBy.PROVIDER
    else:
        raise SessionError('Only session participants may end it.', code='forbidden')
    _state(session, ConsultationSession.Status.IN_PROGRESS)
    effective_end = max(_timestamp(now), session.billing_checked_at or session.started_at)
    wallet = _locked_wallet(session)
    _settle(session, wallet, effective_end, ending=True)
    if session.status != ConsultationSession.Status.COMPLETED:
        _complete(session, effective_end, ended_by, ConsultationSession.EndReason.PARTICIPANT_ENDED)
    return session


def _timestamp(now):
    now = timezone.now() if now is None else now
    if not isinstance(now, datetime) or timezone.is_naive(now):
        raise SessionError('An aware datetime is required.', code='invalid_time')
    return now


def _locked_wallet(session):
    try:
        wallet = Wallet.objects.select_for_update().get(user_id=session.customer_id)
    except Wallet.DoesNotExist:
        raise SessionError('Customer has no wallet.', code='missing_wallet') from None
    if not wallet.balance.is_finite() or wallet.balance < 0:
        raise SessionError('Wallet balance is invalid.', code='invalid_wallet')
    return wallet


def _check_ledger(session, wallet):
    totals = session.charges.aggregate(count=Count('id'), first=Min('minute_number'),
                                       last=Max('minute_number'), amount=Sum('amount'))
    if (session.billed_minutes < 1
            or totals['count'] != session.billed_minutes
            or totals['first'] != 1 or totals['last'] != session.billed_minutes
            or totals['amount'] != session.billed_amount
            or session.charges.exclude(wallet=wallet, amount=session.rate_snapshot).exists()):
        raise SessionError('Session billing history is inconsistent.', code='billing_inconsistent')


def _charge_minute(session, wallet, charged_at):
    rate = _rate(session.rate_snapshot)
    if session.billed_minutes >= 2147483647 or session.billed_amount + rate > Decimal('9999999999.99'):
        raise SessionError('Session billing limit exceeded.', code='billing_limit')
    if wallet.balance < rate:
        return False
    try:
        with transaction.atomic():
            if not Wallet.objects.filter(pk=wallet.pk, balance__gte=rate).update(balance=F('balance') - rate):
                return False
            SessionCharge.objects.create(
                session=session, wallet=wallet, minute_number=session.billed_minutes + 1,
                amount=rate, charged_at=charged_at,
            )
            session.billed_minutes += 1
            session.billed_amount += rate
            session.save(update_fields=['billed_minutes', 'billed_amount'])
    except IntegrityError as error:
        diagnostic = getattr(error.__cause__, 'diag', None)
        if diagnostic and diagnostic.constraint_name == 'session_charge_minute_unique':
            raise SessionError('Charge minute already exists.', code='charge_conflict') from None
        raise
    wallet.balance -= rate
    return True


def _complete(session, ended_at, ended_by, reason):
    session.status = ConsultationSession.Status.COMPLETED
    session.ended_at = ended_at
    session.ended_by = ended_by
    session.end_reason = reason
    session.actual_duration = ended_at - session.started_at
    session.billing_checked_at = ended_at
    session.save(update_fields=['status', 'ended_at', 'ended_by', 'end_reason',
                                'actual_duration', 'billing_checked_at',
                                'billed_minutes', 'billed_amount'])


def _charge_minutes(session, wallet, count, rate, charged_at):
    """Debit once and insert bounded ledger batches; the caller persists counters."""
    amount = rate * count
    try:
        with transaction.atomic():
            updated = Wallet.objects.filter(
                pk=wallet.pk, balance__gte=amount
            ).update(balance=F('balance') - amount)
            if not updated:
                raise SessionError('Wallet debit could not be applied.', code='invalid_wallet')
            first = session.billed_minutes + 1
            stop = first + count
            for batch_start in range(first, stop, 500):
                SessionCharge.objects.bulk_create([
                    SessionCharge(
                        session_id=session.pk, wallet_id=wallet.pk, minute_number=minute,
                        amount=rate, charged_at=charged_at,
                    )
                    for minute in range(batch_start, min(batch_start + 500, stop))
                ], batch_size=500)
    except IntegrityError as error:
        diagnostic = getattr(error.__cause__, 'diag', None)
        if diagnostic and diagnostic.constraint_name == 'session_charge_minute_unique':
            raise SessionError('Charge minute already exists.', code='charge_conflict') from None
        raise
    wallet.balance -= amount
    session.billed_minutes += count
    session.billed_amount += amount


def _settle(session, wallet, now, *, ending=False):
    rate = _rate(session.rate_snapshot)
    _check_ledger(session, wallet)
    elapsed = now - session.started_at
    if elapsed < timedelta(0):
        return
    # Integer timedelta arithmetic preserves the exact microsecond boundary.
    due = (elapsed // timedelta(minutes=1) + 1) if not ending else max(
        1, (elapsed + timedelta(minutes=1) - timedelta(microseconds=1)) // timedelta(minutes=1)
    )
    missing = max(0, due - session.billed_minutes)
    affordable = int(wallet.balance // rate)
    count = min(missing, affordable)
    if (session.billed_minutes + count > 2147483647
            or session.billed_amount + rate * count > Decimal('9999999999.99')):
        raise SessionError('Session billing limit exceeded.', code='billing_limit')
    if count:
        _charge_minutes(session, wallet, count, rate, now)
    if count < missing:
        boundary = session.started_at + timedelta(minutes=session.billed_minutes)
        _complete(session, boundary, ConsultationSession.EndedBy.SYSTEM,
                  ConsultationSession.EndReason.INSUFFICIENT_BALANCE)
        return
    session.billing_checked_at = max(now, session.billing_checked_at or session.started_at)
    if not ending:
        session.save(update_fields=['billing_checked_at', 'billed_minutes', 'billed_amount'])


@transaction.atomic
def bill_session(session_id, now=None):
    """Synchronously settle advance minutes; no scheduler invokes this service."""
    session = _locked_session(session_id)
    now = _timestamp(now)
    if session.status == ConsultationSession.Status.COMPLETED:
        return session
    _state(session, ConsultationSession.Status.IN_PROGRESS)
    wallet = _locked_wallet(session)
    _settle(session, wallet, now)
    return session
