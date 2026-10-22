import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission

from .models import User


class IsCustomer(BasePermission):
    message = 'Only USER accounts may access this endpoint.'

    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == User.Role.USER


class HasRechargeWebhookSecret(BasePermission):
    message = 'Invalid webhook authorization.'

    def has_permission(self, request, view):
        expected = settings.RECHARGE_WEBHOOK_SECRET
        supplied = request.headers.get('X-Recharge-Webhook-Secret', '')
        return bool(expected and supplied) and hmac.compare_digest(
            expected.encode('utf-8'), supplied.encode('utf-8')
        )
