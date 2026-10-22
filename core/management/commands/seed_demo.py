import os
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import ProviderProfile, User
from core.session_services import create_provider_profile


class Command(BaseCommand):
    help = 'Create one local demo provider; requires --local, DJANGO_DEBUG=True, and a password environment variable.'

    def add_arguments(self, parser):
        parser.add_argument('--local', action='store_true')
        parser.add_argument('--username', default='demo_provider')
        parser.add_argument('--rate', default='10.00')
        parser.add_argument('--password-env', default='DEMO_PROVIDER_PASSWORD')

    @transaction.atomic
    def handle(self, *args, **options):
        if not options['local'] or not settings.DEBUG:
            raise CommandError('Demo seeding requires --local and DJANGO_DEBUG=True; never use in production.')
        password = os.environ.get(options['password_env'])
        if not password:
            raise CommandError('The specified demo password environment variable is required.')
        username = User.normalize_username(options['username'])
        candidate = User(username=username, role=User.Role.PROVIDER)
        try:
            User._meta.get_field('username').clean(username, candidate)
            validate_password(password, user=candidate)
            rate = Decimal(options['rate'])
            ProviderProfile._meta.get_field('per_minute_rate').clean(rate, None)
            if rate <= 0:
                raise ValidationError('Rate must be positive.')
        except (ValidationError, InvalidOperation, ValueError):
            raise CommandError('Invalid demo username, password, or positive two-decimal rate.') from None
        existing = User.objects.filter(username=username).first()
        if existing:
            profile = ProviderProfile.objects.filter(user=existing).first()
            if (existing.role != User.Role.PROVIDER or not existing.is_active or profile is None
                    or profile.per_minute_rate != rate or not existing.check_password(password)):
                raise CommandError('Existing account conflicts with demo configuration; no changes made.')
            self.stdout.write(f'Demo provider already configured. User ID: {existing.pk}')
            return
        provider = User.objects.create_user(username=username, password=password, role=User.Role.PROVIDER)
        create_provider_profile(provider, rate)
        self.stdout.write(self.style.SUCCESS(f'Demo provider created. User ID: {provider.pk}'))
