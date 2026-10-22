from collections.abc import Mapping
from decimal import Decimal

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import ConsultationSession, RechargeTransaction, User
from .services import register_customer


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError({
                    field: ['Unexpected field.'] for field in sorted(unknown)
                })
        return super().to_internal_value(data)


class RegistrationSerializer(StrictSerializer):
    username = serializers.CharField(
        max_length=150, validators=User._meta.get_field('username').validators
    )
    password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_username(self, value):
        value = User.normalize_username(value)
        try:
            User._meta.get_field('username').run_validators(value)
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.messages) from None
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError('A user with that username already exists.')
        return value

    def validate(self, attrs):
        try:
            validate_password(attrs['password'], user=User(username=attrs['username']))
        except DjangoValidationError as error:
            raise serializers.ValidationError({'password': error.messages}) from None
        return attrs

    def create(self, validated_data):
        return register_customer(**validated_data)


class RechargeInitiationSerializer(StrictSerializer):
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal('0.01')
    )


class RechargeCallbackSerializer(StrictSerializer):
    reference = serializers.UUIDField()
    outcome = serializers.ChoiceField(choices=[
        RechargeTransaction.Status.SUCCESS, RechargeTransaction.Status.FAILED,
    ])


class EmptyActionSerializer(StrictSerializer):
    pass


class SessionAssignmentSerializer(StrictSerializer):
    provider_id = serializers.PrimaryKeyRelatedField(
        source='provider', queryset=User.objects.filter(role=User.Role.PROVIDER, is_active=True),
        pk_field=serializers.IntegerField(),
    )


class SessionDetailSerializer(serializers.ModelSerializer):
    provider_id = serializers.SerializerMethodField()
    billed_duration = serializers.DurationField(read_only=True)
    actual_duration_seconds = serializers.FloatField(read_only=True)

    def get_provider_id(self, session):
        return session.provider.user_id if session.provider_id else None

    class Meta:
        model = ConsultationSession
        fields = [
            'id', 'customer_id', 'provider_id', 'status', 'rate_snapshot', 'created_at',
            'assigned_at', 'accepted_at', 'started_at', 'ended_at', 'ended_by',
            'end_reason', 'billed_minutes', 'billed_amount', 'billed_duration',
            'actual_duration', 'actual_duration_seconds',
        ]
        read_only_fields = fields
