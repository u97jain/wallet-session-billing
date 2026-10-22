from decimal import Decimal
from datetime import timedelta
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        USER = 'USER', 'User'
        PROVIDER = 'PROVIDER', 'Provider'

    role = models.CharField(max_length=8, choices=Role.choices, default=Role.USER)

    class Meta(AbstractUser.Meta):
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=['USER', 'PROVIDER']),
                name='user_role_valid',
            ),
        ]


class Wallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='wallet'
    )
    balance = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00')
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(balance__gte=0), name='wallet_balance_nonnegative'
            ),
        ]


class RechargeTransaction(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SUCCESS = 'success', 'Success'
        FAILED = 'failed', 'Failed'

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='recharges'
    )
    wallet = models.ForeignKey(
        Wallet, on_delete=models.PROTECT, related_name='recharges'
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(
        max_length=7, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name='recharge_amount_positive'
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status='pending', completed_at__isnull=True)
                    | models.Q(status__in=['success', 'failed'], completed_at__isnull=False)
                ),
                name='recharge_status_completion_valid',
            ),
        ]


class ProviderProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='provider_profile'
    )
    per_minute_rate = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(per_minute_rate__gt=0), name='provider_rate_positive'
            ),
        ]


class ConsultationSession(models.Model):
    class Status(models.TextChoices):
        UNASSIGNED = 'unassigned', 'Unassigned'
        ASSIGNED = 'assigned', 'Assigned'
        ACCEPTED_BY_PROVIDER = 'accepted_by_provider', 'Accepted by provider'
        IN_PROGRESS = 'in_progress', 'In progress'
        COMPLETED = 'completed', 'Completed'

    class EndedBy(models.TextChoices):
        CUSTOMER = 'customer', 'Customer'
        PROVIDER = 'provider', 'Provider'
        SYSTEM = 'system', 'System'

    class EndReason(models.TextChoices):
        PARTICIPANT_ENDED = 'participant_ended', 'Participant ended'
        INSUFFICIENT_BALANCE = 'insufficient_balance', 'Insufficient balance'

    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='consultation_sessions'
    )
    provider = models.ForeignKey(
        ProviderProfile, null=True, blank=True, on_delete=models.PROTECT, related_name='sessions'
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UNASSIGNED)
    rate_snapshot = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_by = models.CharField(max_length=8, choices=EndedBy.choices, blank=True, default='')
    end_reason = models.CharField(max_length=20, choices=EndReason.choices, blank=True, default='')
    billed_minutes = models.PositiveIntegerField(default=0)
    billed_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    actual_duration = models.DurationField(default=timedelta)
    billing_checked_at = models.DateTimeField(null=True, blank=True)

    @property
    def billed_duration(self):
        return timedelta(minutes=self.billed_minutes)

    @property
    def actual_duration_seconds(self):
        return self.actual_duration.total_seconds()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['customer'], condition=models.Q(status='in_progress'),
                name='session_one_in_progress_customer',
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=[
                    'unassigned', 'assigned', 'accepted_by_provider', 'in_progress', 'completed',
                ]), name='session_status_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status='unassigned', provider__isnull=True, assigned_at__isnull=True)
                    | models.Q(status__in=['assigned', 'accepted_by_provider', 'in_progress', 'completed'],
                               provider__isnull=False, assigned_at__isnull=False)
                ), name='session_assignment_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status__in=['unassigned', 'assigned'], accepted_at__isnull=True)
                    | models.Q(status__in=['accepted_by_provider', 'in_progress', 'completed'], accepted_at__isnull=False)
                ), name='session_acceptance_valid',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status__in=['unassigned', 'assigned', 'accepted_by_provider'],
                               started_at__isnull=True, rate_snapshot__isnull=True)
                    | models.Q(status__in=['in_progress', 'completed'], started_at__isnull=False,
                               rate_snapshot__isnull=False, rate_snapshot__gt=0)
                ), name='session_start_valid',
            ),
            models.CheckConstraint(
                condition=(
                    (~models.Q(status='completed') & models.Q(
                        ended_at__isnull=True, ended_by='', end_reason='', actual_duration=timedelta(0)))
                    | (models.Q(status='completed', ended_at__isnull=False) & (
                        models.Q(ended_by__in=['customer', 'provider'], end_reason='participant_ended')
                        | models.Q(ended_by='system', end_reason='insufficient_balance')
                    ))
                ), name='session_completion_valid',
            ),
            models.CheckConstraint(
                condition=(
                    (models.Q(assigned_at__isnull=True) | models.Q(assigned_at__gte=models.F('created_at')))
                    & (models.Q(accepted_at__isnull=True) | models.Q(accepted_at__gte=models.F('assigned_at')))
                    & (models.Q(started_at__isnull=True) | models.Q(started_at__gte=models.F('accepted_at')))
                    & (models.Q(ended_at__isnull=True) | models.Q(ended_at__gte=models.F('started_at')))
                ), name='session_timestamps_ordered',
            ),
            models.CheckConstraint(
                condition=models.Q(billed_amount__gte=0, actual_duration__gte=timedelta(0)),
                name='session_totals_nonnegative',
            ),
            models.CheckConstraint(
                condition=(models.Q(started_at__isnull=False)
                           | models.Q(billed_minutes=0, billed_amount=0)),
                name='session_unstarted_unbilled',
            ),
        ]


class SessionCharge(models.Model):
    session = models.ForeignKey(
        ConsultationSession, on_delete=models.PROTECT, related_name='charges'
    )
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name='session_charges')
    minute_number = models.PositiveIntegerField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    charged_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['session', 'minute_number'], name='session_charge_minute_unique'),
            models.CheckConstraint(condition=models.Q(minute_number__gte=1), name='session_charge_minute_positive'),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0, amount__lte=Decimal('9999999999.99')),
                name='session_charge_amount_positive',
            ),
        ]
