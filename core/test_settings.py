import os
import runpy
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError
from django.core.management.commands.runserver import Command
from django.test import Client, SimpleTestCase, override_settings


class EnvironmentSettingsTests(SimpleTestCase):
    def settings_from(self, **values):
        environment = dict(DJANGO_SECRET_KEY='synthetic-settings-test', DB_NAME='synthetic',
                           DB_USER='synthetic', DB_PASSWORD='synthetic-password')
        environment.update(values)
        environment = {key: value for key, value in environment.items() if value is not None}
        with patch.dict(os.environ, environment, clear=True), patch('dotenv.load_dotenv'):
            return runpy.run_path(str(Path(__file__).resolve().parent.parent / 'config/settings.py'))

    def test_debug_defaults_false_and_boolean_values_are_explicit(self):
        self.assertFalse(self.settings_from()['DEBUG'])
        for value, expected in [('True', True), ('true', True), (' TRUE ', True),
                                ('False', False), ('false', False)]:
            self.assertIs(self.settings_from(DJANGO_DEBUG=value)['DEBUG'], expected)
        for value in ['', '1', '0', 'yes', 'no', 'synthetic-invalid-value']:
            with self.assertRaises(ImproperlyConfigured) as error:
                self.settings_from(DJANGO_DEBUG=value)
            self.assertEqual(str(error.exception), 'DJANGO_DEBUG must be True or False.')

    def test_generic_debug_variable_does_not_influence_django(self):
        self.assertFalse(self.settings_from(DEBUG='1')['DEBUG'])
        self.assertTrue(self.settings_from(DEBUG='1', DJANGO_DEBUG='True')['DEBUG'])
        self.assertFalse(self.settings_from(DEBUG='1', DJANGO_DEBUG='False')['DEBUG'])
        with self.assertRaises(ImproperlyConfigured):
            self.settings_from(DEBUG='True', DJANGO_DEBUG='1')

    def test_required_database_settings_fail_without_disclosing_values(self):
        for name in ['DB_NAME', 'DB_USER', 'DB_PASSWORD']:
            for value in [None, '']:
                with self.subTest(name=name, missing=value is None):
                    with self.assertRaises(ImproperlyConfigured) as error:
                        self.settings_from(**{name: value})
                    self.assertIn(name, str(error.exception))
                    self.assertNotIn('synthetic-password', str(error.exception))
        with self.assertRaises(ImproperlyConfigured) as error:
            self.settings_from(DB_NAME=None, DB_USER=None, DB_PASSWORD=None)
        self.assertIn('DB_NAME, DB_USER, DB_PASSWORD', str(error.exception))

    def test_database_and_host_configuration_is_preserved(self):
        settings = self.settings_from(DJANGO_ALLOWED_HOSTS=' localhost, 127.0.0.1, [::1], ')
        self.assertEqual(settings['ALLOWED_HOSTS'], ['localhost', '127.0.0.1', '[::1]'])
        self.assertEqual(self.settings_from()['ALLOWED_HOSTS'], [])
        self.assertEqual(self.settings_from(ALLOWED_HOSTS='unrelated.example')['ALLOWED_HOSTS'], [])
        database = settings['DATABASES']['default']
        self.assertEqual(database['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(database['HOST'], 'localhost')
        self.assertEqual(database['PORT'], '5432')
        overrides = self.settings_from(DB_HOST='synthetic-host', DB_PORT='6543')['DATABASES']['default']
        self.assertEqual((overrides['HOST'], overrides['PORT']), ('synthetic-host', '6543'))

    def test_local_runserver_requires_hosts_when_debug_is_false(self):
        command = Command()
        command.run = Mock()
        with override_settings(DEBUG=False, ALLOWED_HOSTS=[]), self.assertRaises(CommandError):
            command.handle(addrport='127.0.0.1:8000', use_ipv6=False)
        with override_settings(DEBUG=False, ALLOWED_HOSTS=['localhost', '127.0.0.1']):
            command.handle(addrport='127.0.0.1:8000', use_ipv6=False)
        command.run.assert_called_once()

    def test_test_client_host_checks_work_with_debug_false(self):
        with override_settings(DEBUG=False, ALLOWED_HOSTS=['testserver']):
            self.assertEqual(Client().get('/api/wallet/').status_code, 401)
        with override_settings(DEBUG=False, ALLOWED_HOSTS=['localhost']):
            self.assertEqual(Client().get('/api/wallet/', HTTP_HOST='localhost').status_code, 401)
            self.assertEqual(Client().get('/api/wallet/', HTTP_HOST='untrusted.example').status_code, 400)
