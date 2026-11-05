import isolation  # noqa: F401  (first: keeps the tests away from real data)
import os
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import cli  # noqa: E402


class SavedKeys(unittest.TestCase):
    def setUp(self):
        self.conf = patch.dict(cli.config_tool.KEYS, {'openrouter': ('X', 'sk-or-', ''), 'cursor': ('Y', None, ''), 'grok': ('Z', 'xai-', '')})
        self.conf.start()
        self.addCleanup(self.conf.stop)

    def _status(self, code):
        error = urllib.error.HTTPError('u', code, 'm', {}, None)
        return patch.object(cli.health.urllib.request, 'urlopen', side_effect=error)

    def test_wrong_format_is_reported_without_calling_the_provider(self):
        with patch.object(cli.health.urllib.request, 'urlopen') as called:
            self.assertIn('formato inválido', cli.health.key_problem('openrouter', 'abc'))
        called.assert_not_called()

    def test_rejected_key_is_reported(self):
        with self._status(401):
            self.assertIn('HTTP 401', cli.health.key_problem('openrouter', 'sk-or-x'))

    def test_other_http_errors_say_nothing_about_the_key(self):
        with self._status(500):
            self.assertIsNone(cli.health.key_problem('openrouter', 'sk-or-x'))

    def test_providers_that_reject_with_400(self):
        with self._status(400):
            self.assertIn('HTTP 400', cli.health.key_problem('grok', 'xai-x'))
            self.assertIsNone(cli.health.key_problem('openrouter', 'sk-or-x'))

    def test_offline_is_not_a_problem(self):
        with patch.object(cli.health.urllib.request, 'urlopen', side_effect=OSError('offline')):
            self.assertIsNone(cli.health.key_problem('openrouter', 'sk-or-x'))

    def test_valid_key_and_uncheckable_provider(self):
        with patch.object(cli.health.urllib.request, 'urlopen') as called:
            self.assertIsNone(cli.health.key_problem('openrouter', 'sk-or-x'))
            self.assertIsNone(cli.health.key_problem('cursor', 'anything'))
        self.assertEqual(called.call_count, 1)

    def test_doctor_lists_the_problem_but_never_the_key(self):
        lines = []
        with patch.object(cli.config_tool, 'stored_keys', side_effect=lambda p: [('work', 'sk-or-secret', 'salva')] if p == 'openrouter' else []), \
                self._status(401), patch.object(cli.health, 'say', side_effect=lambda *a: lines.append(a[0] if a else '')), \
                patch.object(cli.health, 'installed', return_value=False):
            cli.doctor()
        text = '\n'.join(lines)
        self.assertIn('work de openrouter', text)
        self.assertNotIn('sk-or-secret', text)


class DoctorTests(unittest.TestCase):
    """`ai doctor` reports what is configured and fails only when nothing can answer."""

    def report(self, profiles, keys):
        lines = []
        ordered = list(profiles.items())
        with patch.object(cli.config_tool, 'ordered', return_value=ordered), \
                patch.object(cli.config_tool, 'profiles', return_value=profiles), \
                patch.object(cli.config_tool, 'KEYS', ['claude', 'openrouter', 'groq', 'cerebras']), \
                patch.object(cli.config_tool, 'stored_key', side_effect=lambda p: (('x', 'name', 'file') if p in keys else (None, None, None))), \
                patch.object(cli.health, 'cooldown_left', return_value=0), \
                patch.object(cli.health, 'installed', return_value=False), \
                patch.object(cli.health, 'version', return_value='1.0'), \
                patch.object(cli.health, 'say', side_effect=lambda *a: lines.append(a[0] if a else '')):
            code = cli.doctor()
        return code, '\n'.join(lines)

    def test_healthy_with_a_logged_account(self):
        code, text = self.report({'primary': {'enabled': True, 'config_dir': '/tmp/x'}}, keys=[])
        self.assertEqual(code, 0)
        self.assertIn('logado', text)

    def test_healthy_with_only_an_api_key(self):
        code, text = self.report({'primary': {'enabled': True}}, keys=['openrouter'])
        self.assertEqual(code, 0)

    def test_fails_with_no_login_and_no_key(self):
        code, text = self.report({'primary': {'enabled': True}}, keys=[])
        self.assertEqual(code, 1)
        self.assertIn('sem login', text)

    def test_dispatch_routes_doctor(self):
        with patch.object(cli, 'doctor', return_value=0) as doc:
            self.assertEqual(cli.main(['--doctor']), 0)
            self.assertEqual(cli.main(['doctor']), 0)
        self.assertEqual(doc.call_count, 2)

    def test_chat_slash_doctor_maps_to_the_command(self):
        from omni_ai import chat
        self.assertEqual(chat.TOOL_COMMANDS['/doctor'], ['--doctor'])
        self.assertIn('/doctor', chat.BUILTIN)


if __name__ == '__main__':
    unittest.main()
