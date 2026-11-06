import isolation  # noqa: F401  (first: keeps the tests away from real data)
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

from test_admin import ADMIN_KEY, AdminCase
from test_server import ROOT

sys.path.insert(0, str(ROOT))
from omni_ai import config  # noqa: E402
from omni_ai.server import logins  # noqa: E402
from omni_ai.server.protocol import RequestError  # noqa: E402

# Fakes print what the real CLIs printed in a container, with fake codes
CLAUDE = """#!/bin/sh
echo "Opening browser to sign in"
printf 'If the browser did not open, visit: https://claude.com/oauth?code=challenge\\nPaste code here if prompted > '
read code
[ "$code" = good-code ] && { echo "login token-from-provider"; exit 0; }
echo "invalid $code"; exit 1
"""
DEVICE = """#!/bin/sh
echo "secret-token-output"
echo "To sign in, open https://auth.example/device"
echo "Enter this one-time code: ABCD-EFGH1"
while [ ! -f "$FAKE_DONE" ]; do sleep 0.1; done
exit 0
"""
CURSOR = """#!/bin/sh
echo "navigate to this link: https://cursor.example/login?challenge=x"
while [ ! -f "$FAKE_DONE" ]; do sleep 0.1; done
exit 0
"""


class Flow(unittest.TestCase):
    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.done = self.root / 'done'
        env = {'FAKE_DONE': str(self.done), 'HOME': tmp.name}
        for tool, body in (('claude', CLAUDE), ('codex', DEVICE), ('grok', DEVICE), ('cursor', CURSOR)):
            path = self.root / tool
            path.write_text(body)
            path.chmod(0o700)
            env[f'{tool.upper()}_BIN'] = str(path)
        self.dir = self.root / 'acct'
        profiles = {'profiles': {'primary': {'priority': 1}, 'second': {'priority': 2, 'config_dir': str(self.dir)},
                                 'legacy': {'priority': 3, 'credentials_file': str(self.root / 'x.json')}}}
        for item in (patch.dict(os.environ, env), patch.object(config, 'profiles', lambda: profiles),
                     patch.object(config, 'share_claude_setup', lambda folder: self.shared.append(folder))):
            item.start()
            self.addCleanup(item.stop)
        self.shared = []
        self.logins = logins.Logins()
        self.addCleanup(self.logins.stop_all)

    def error(self, call, *args):
        with self.assertRaises(RequestError) as caught:
            call(*args)
        return caught.exception

    def test_claude_with_the_right_code(self):
        started = self.logins.start('primary')
        self.assertTrue(started['needs_code'])
        self.assertEqual(started['authorization_url'], 'https://claude.com/oauth?code=challenge')
        status, body = self.logins.complete('primary', {'attempt_id': started['attempt_id'], 'code': 'good-code'})
        self.assertEqual((status, body['status']), (200, 'completed'))
        self.assertNotIn('token', json.dumps(body))

    def test_invalid_code_is_reported_without_echoing_it(self):
        started = self.logins.start('primary')
        error = self.error(self.logins.complete, 'primary', {'attempt_id': started['attempt_id'], 'code': 'wrong-code'})
        self.assertEqual(error.status, 400)
        self.assertNotIn('wrong-code', error.message)
        self.assertEqual(self.error(self.logins.complete, 'primary', {'attempt_id': started['attempt_id'], 'code': 'x'}).status, 404)

    def test_tool_that_keeps_asking_after_a_wrong_code(self):
        Path(os.environ['CLAUDE_BIN']).write_text(
            '#!/bin/sh\necho "visit https://claude.com/oauth"\nwhile read code; do\n'
            '  [ "$code" = good-code ] && exit 0\n  echo "Invalid code. Please make sure the full code was copied."\ndone\n')
        started = self.logins.start('primary')
        began = time.monotonic()
        error = self.error(self.logins.complete, 'primary', {'attempt_id': started['attempt_id'], 'code': 'wrong-code'})
        self.assertEqual((error.status, error.code), (400, 'invalid_code'))
        self.assertLess(time.monotonic() - began, 5)
        status, _ = self.logins.complete('primary', {'attempt_id': started['attempt_id'], 'code': 'good-code'})
        self.assertEqual(status, 200)

    def test_extra_account_uses_its_folder_and_shares_the_setup(self):
        started = self.logins.start('second')
        self.logins.complete('second', {'attempt_id': started['attempt_id'], 'code': 'good-code'})
        self.assertEqual(self.shared, [self.dir])

    def test_code_that_is_not_one_line(self):
        started = self.logins.start('primary')
        for code in ('a\nb', '', 1, None, 'x' * 3000):
            self.assertEqual(self.error(self.logins.complete, 'primary', {'attempt_id': started['attempt_id'], 'code': code}).status, 400)

    def test_device_flow_pending_then_done(self):
        for tool in ('codex', 'grok'):
            started = self.logins.start(tool)
            self.assertEqual(started['user_code'], 'ABCD-EFGH1')
            self.assertFalse(started['needs_code'])
            self.assertEqual(self.logins.complete(tool, {'attempt_id': started['attempt_id']})[0], 202)
            self.done.touch()
            deadline = time.time() + 5
            while time.time() < deadline and self.logins.attempts[tool].process.poll() is None:
                time.sleep(0.05)
            time.sleep(0.2)
            self.assertEqual(self.logins.complete(tool, {'attempt_id': started['attempt_id']})[0], 200)
            self.done.unlink()

    def test_cursor_has_no_user_code(self):
        started = self.logins.start('cursor')
        self.assertNotIn('user_code', started)
        self.assertEqual(self.error(self.logins.complete, 'cursor', {'attempt_id': started['attempt_id'], 'code': 'x'}).status, 400)

    def test_codex_always_uses_device_auth(self):
        self.assertEqual(logins._command('codex')[1:], ['login', '--device-auth'])

    def test_one_attempt_per_account(self):
        first = self.logins.start('codex')
        self.assertEqual(self.error(self.logins.start, 'codex').status, 409)
        self.logins.start('cursor')
        self.logins.cancel('codex', first['attempt_id'])
        self.assertIn('attempt_id', self.logins.start('codex'))

    def test_wrong_attempt(self):
        self.logins.start('codex')
        for wrong in ('login_wrong', None, 1):
            self.assertEqual(self.error(self.logins.complete, 'codex', {'attempt_id': wrong}).status, 404)
            self.assertEqual(self.error(self.logins.cancel, 'codex', wrong).status, 404)

    def test_expired_attempt_is_refused_and_stops_the_process(self):
        started = self.logins.start('codex')
        attempt = self.logins.attempts['codex']
        attempt.expires = 0
        self.assertEqual(self.error(self.logins.complete, 'codex', {'attempt_id': started['attempt_id']}).status, 410)
        attempt.process.wait(timeout=5)

    def test_timer_stops_an_abandoned_attempt(self):
        self.logins.start('codex')
        attempt = self.logins.attempts['codex']
        self.logins._expire('codex', attempt)
        attempt.process.wait(timeout=5)
        self.assertNotIn('codex', self.logins.attempts)

    def test_shutdown_stops_attempts(self):
        self.logins.start('codex')
        attempt = self.logins.attempts['codex']
        self.logins.stop_all()
        attempt.process.wait(timeout=5)

    def test_unsupported(self):
        for account, status in (('agy', 400), ('openrouter', 400), ('legacy', 400), ('ghost', 404), ('codex.ghost', 404)):
            self.assertEqual(self.error(self.logins.start, account).status, status, account)

    def test_tool_without_a_link_fails_clearly(self):
        Path(os.environ['CURSOR_BIN']).write_text('#!/bin/sh\necho nothing\nexit 1\n')
        self.assertEqual(self.error(self.logins.start, 'cursor').status, 502)

    def test_not_installed(self):
        with patch.dict(os.environ, {'CURSOR_BIN': str(self.root / 'none')}):
            self.assertEqual(self.error(self.logins.start, 'cursor').status, 400)

    def test_secrets_do_not_reach_the_login_process(self):
        Path(os.environ['CURSOR_BIN']).write_text('#!/bin/sh\necho "https://x.example/${SMART_AI_ADMIN_KEY:-clean}"\nsleep 5\n')
        with patch.dict(os.environ, {'SMART_AI_ADMIN_KEY': 'secret'}):
            self.assertTrue(self.logins.start('cursor')['authorization_url'].endswith('/clean'))


class OverHttp(AdminCase):
    def test_needs_the_admin_key(self):
        for path in ('/admin/accounts/codex/login', '/admin/accounts/codex/login/complete'):
            self.assertEqual(self.admin('POST', path, {}, key=self.key)[0], 401)
            self.assertEqual(self.admin('POST', path, {}, key='wrong')[0], 401)
        self.assertEqual(self.admin('DELETE', '/admin/accounts/codex/login', key=self.key)[0], 401)

    def test_not_available_when_disabled(self):
        response, _ = self.call('POST', '/admin/accounts/codex/login', {})
        self.assertEqual(response.status, 404)

    def test_flow_and_log_stay_clean(self):
        fake = self.root / 'codex'
        original = fake.read_text()
        self.addCleanup(fake.write_text, original)
        fake.write_text('#!/bin/sh\necho "open https://auth.example/device"\necho "code ABCD-EFGH1"\necho secret-output\nexit 0\n')
        status, body = self.admin('POST', '/admin/accounts/codex/login', {})
        self.assertEqual((status, body['user_code']), (201, 'ABCD-EFGH1'))
        self.assertNotIn('secret-output', json.dumps(body))
        time.sleep(0.3)
        status, body = self.admin('POST', '/admin/accounts/codex/login/complete', {'attempt_id': body['attempt_id']})
        self.assertEqual((status, body['status']), (200, 'completed'))

    def test_cancel_needs_the_attempt(self):
        self.assertEqual(self.admin('DELETE', '/admin/accounts/codex/login')[0], 404)
        self.assertEqual(self.admin('DELETE', '/admin/accounts/codex/login', headers={'X-Omni-Login-Attempt': 'x'})[0], 404)

    def test_account_id_cannot_escape(self):
        self.assertEqual(self.admin('POST', '/admin/accounts/..%2f..%2fetc/login', {})[0], 404)


if __name__ == '__main__':
    unittest.main()
