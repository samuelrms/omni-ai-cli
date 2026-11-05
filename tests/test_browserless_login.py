"""Login without a browser: which command each tool gets, and logging in to the main accounts."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import contextlib
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import osutils  # noqa: E402
from omni_ai.config import accounts  # noqa: E402


class Headless(unittest.TestCase):
    def detect(self, env, platform='darwin', docker=False):
        clean = {k: v for k, v in os.environ.items()
                 if k not in ('SMART_AI_HEADLESS', 'SSH_CONNECTION', 'SSH_TTY', 'DISPLAY', 'WAYLAND_DISPLAY')}
        with patch.dict(os.environ, {**clean, **env}, clear=True), patch.object(sys, 'platform', platform), \
                patch.object(Path, 'exists', return_value=docker):
            return osutils.headless()

    def test_detection(self):
        self.assertFalse(self.detect({}))
        self.assertTrue(self.detect({'SSH_CONNECTION': '1 2 3 4'}))
        self.assertTrue(self.detect({}, docker=True))
        self.assertTrue(self.detect({}, platform='linux'))
        self.assertFalse(self.detect({'DISPLAY': ':0'}, platform='linux'))

    def test_override_wins(self):
        self.assertFalse(self.detect({'SMART_AI_HEADLESS': '0', 'SSH_TTY': 'x'}))
        self.assertTrue(self.detect({'SMART_AI_HEADLESS': '1'}))


class Commands(unittest.TestCase):
    def test_only_codex_needs_the_device_flag(self):
        with patch.object(accounts, 'headless', return_value=True):
            self.assertEqual(accounts.login_command('codex'), ['codex', 'login', '--device-auth'])
            self.assertEqual(accounts.login_command('grok'), ['grok', 'login'])
            self.assertEqual(accounts.login_command('claude'), ['claude', 'auth', 'login'])
        with patch.object(accounts, 'headless', return_value=False):
            self.assertEqual(accounts.login_command('codex'), ['codex', 'login'])

    def run_account(self, *args):
        calls = []
        with patch.object(accounts, 'headless', return_value=True), patch.object(accounts, 'resolve_bin', lambda name: name), \
                patch.object(accounts.subprocess, 'call', lambda argv, **kw: calls.append((argv, kw)) or 0), \
                contextlib.redirect_stdout(io.StringIO()):
            accounts.cmd_account(list(args))
        return calls

    def test_main_logins(self):
        for args, expected in ((('login', 'primary'), ['claude', 'auth', 'login']),
                               (('login', 'claude'), ['claude', 'auth', 'login']),
                               (('login', 'codex'), ['codex', 'login', '--device-auth']),
                               (('login', 'grok'), ['grok', 'login']),
                               (('login', 'cursor'), ['cursor-agent', 'login'])):
            self.assertEqual(self.run_account(*args)[0][0], expected)

    def test_login_without_a_target_explains_the_options(self):
        with self.assertRaises(SystemExit) as caught:
            accounts.cmd_account(['login'])
        self.assertIn('primary', str(caught.exception))

    def test_extra_codex_account_uses_the_device_flag_in_its_own_home(self):
        calls = []
        with patch.object(accounts, 'headless', return_value=True), patch.object(accounts, 'resolve_bin', lambda name: name), \
                patch.object(accounts, 'share_tool_setup', lambda *a: None), \
                patch.object(accounts.subprocess, 'call', lambda argv, **kw: calls.append((argv, kw)) or 0), \
                patch.object(Path, 'mkdir'), contextlib.redirect_stdout(io.StringIO()):
            accounts.tool_login('codex', 'work', {'config_dir': '/x/work'})
        self.assertEqual(calls[0][0], ['codex', 'login', '--device-auth'])
        self.assertEqual(calls[0][1]['env']['CODEX_HOME'], '/x/work')

    def test_failed_login_is_reported(self):
        with patch.object(accounts, 'headless', return_value=False), patch.object(accounts, 'resolve_bin', lambda name: name), \
                patch.object(accounts.subprocess, 'call', return_value=3), contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as caught:
            accounts.cmd_account(['login', 'grok'])
        self.assertEqual(caught.exception.code, 3)


if __name__ == '__main__':
    unittest.main()
