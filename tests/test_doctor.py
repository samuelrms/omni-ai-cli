import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import cli  # noqa: E402


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
