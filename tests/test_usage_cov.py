"""Branch coverage for omni_ai.usage: account probes, limit math, cache and snapshot.

Network, subprocess and the Keychain are all stubbed; the cache file lives in a
temp dir, so nothing here touches the machine or the real services.
"""
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import usage  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeProc:
    """Minimal stand-in for `codex app-server` speaking the JSON-RPC lines back."""

    def __init__(self, lines):
        self.stdin = io.StringIO()
        self._lines = iter(lines)
        self.stdout = self
        self.killed = False

    def readline(self):
        return next(self._lines, '')

    def kill(self):
        self.killed = True


class PlanAndFormatTests(unittest.TestCase):
    def test_plan_name_known_and_capitalised(self):
        self.assertEqual(usage.plan_name('pro'), 'Pro')
        self.assertEqual(usage.plan_name('custom'), 'Custom')
        self.assertEqual(usage.plan_name(''), '')

    def test_window_label_variants(self):
        self.assertEqual(usage.window_label(300, label='opus'), 'opus')
        self.assertEqual(usage.window_label(10080), 'semana')
        self.assertEqual(usage.window_label(2880), '2 dias')
        self.assertEqual(usage.window_label(120), '2h')
        self.assertEqual(usage.window_label(0), 'crédito')

    def test_clock_today_and_weekday(self):
        soon = time.time() + 3600
        self.assertRegex(usage.clock(soon), r'^\d{2}:\d{2}$')
        later = time.time() + 3 * 86400
        self.assertRegex(usage.clock(later), r'^(seg|ter|qua|qui|sex|sáb|dom) \d{2}:\d{2}$')


class ClaudeAccountTests(unittest.TestCase):
    def test_returns_email_and_plan(self):
        with patch.object(usage.subprocess, 'run',
                          return_value=type('R', (), {'stdout': '{"loggedIn": true, "email": "a@b", "subscriptionType": "max"}'})()):
            self.assertEqual(usage.claude_account({}), {'email': 'a@b', 'plan': 'Max'})

    def test_none_when_not_logged_in(self):
        with patch.object(usage.subprocess, 'run',
                          return_value=type('R', (), {'stdout': '{"loggedIn": false}'})()):
            self.assertIsNone(usage.claude_account({'config_dir': '~/x'}))

    def test_none_on_error(self):
        with patch.object(usage.subprocess, 'run', side_effect=OSError):
            self.assertIsNone(usage.claude_account({}))


class CodexStatusTests(unittest.TestCase):
    def test_parses_account_and_windows(self):
        lines = [
            'not json\n',
            json.dumps({'id': 1, 'result': {}}) + '\n',
            json.dumps({'id': 2, 'result': {'account': {'email': 'c@d', 'planType': 'pro'}}}) + '\n',
            json.dumps({'id': 3, 'result': {'rateLimits': {
                'primary': {'usedPercent': 42.0, 'windowDurationMins': 300, 'resetsAt': 123},
                'secondary': {'usedPercent': None},
                'rateLimitReachedType': 'primary'}}}) + '\n',
        ]
        with patch.object(usage.subprocess, 'Popen', return_value=FakeProc(lines)), \
                patch.object(usage.select, 'select', side_effect=lambda r, w, x, t=0: (r, [], [])):
            status = usage.codex_status()
        self.assertEqual(status['email'], 'c@d')
        self.assertEqual(status['plan'], 'Pro')
        self.assertTrue(status['blocked'])
        self.assertEqual(len(status['windows']), 1)
        self.assertEqual(status['windows'][0]['used'], 42.0)

    def test_none_when_binary_missing(self):
        with patch.object(usage.subprocess, 'Popen', side_effect=OSError):
            self.assertIsNone(usage.codex_status())

    def test_none_when_no_answers(self):
        with patch.object(usage.subprocess, 'Popen', return_value=FakeProc([])), \
                patch.object(usage.select, 'select', return_value=([], [], [])):
            self.assertIsNone(usage.codex_status(timeout=0.05))


class OpenRouterTests(unittest.TestCase):
    def test_credits_parsed(self):
        payload = {'data': {'usage': 1.5, 'limit': 10, 'is_free_tier': True}}
        with patch.object(usage.urllib.request, 'urlopen', return_value=FakeResponse(payload)):
            self.assertEqual(usage.openrouter_credits('sk-or-x'), {'used': 1.5, 'limit': 10, 'free': True})

    def test_none_on_network_error(self):
        with patch.object(usage.urllib.request, 'urlopen', side_effect=OSError):
            self.assertIsNone(usage.openrouter_credits('sk-or-x'))


class ClaudeTokenTests(unittest.TestCase):
    def test_reads_credentials_file(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': 'tok-123'}}))
            with patch.object(usage.sys, 'platform', 'linux'):
                self.assertEqual(usage.claude_token({'config_dir': str(folder)}), 'tok-123')

    def test_none_when_file_absent(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(usage.sys, 'platform', 'linux'):
                self.assertIsNone(usage.claude_token({'config_dir': d}))

    def test_keychain_on_darwin(self):
        raw = json.dumps({'claudeAiOauth': {'accessToken': 'mac-tok'}})
        with patch.object(usage.sys, 'platform', 'darwin'), \
                patch.object(usage.subprocess, 'run', return_value=type('R', (), {'stdout': raw})()):
            self.assertEqual(usage.claude_token({'config_dir': '/tmp/whatever'}), 'mac-tok')


class ClaudeLimitsTests(unittest.TestCase):
    def test_none_without_token(self):
        with patch.object(usage, 'claude_token', return_value=None):
            self.assertIsNone(usage.claude_limits({}))

    def test_parses_windows(self):
        data = {'five_hour': {'utilization': 50.0, 'resets_at': '2026-10-27T18:00:00Z'},
                'seven_day_opus': {'utilization': 12.0, 'resets_at': None}}
        with patch.object(usage, 'claude_token', return_value='tok'), \
                patch.object(usage.urllib.request, 'urlopen', return_value=FakeResponse(data)):
            windows = usage.claude_limits({})
        labels = {w.get('label') for w in windows}
        self.assertIn('opus', labels)

    def test_rate_limited_returns_retry(self):
        err = usage.urllib.error.HTTPError('u', 429, 'too many', {'retry-after': '55'}, None)
        with patch.object(usage, 'claude_token', return_value='tok'), \
                patch.object(usage.urllib.request, 'urlopen', side_effect=err):
            self.assertEqual(usage.claude_limits({}), {'error': 'rate', 'retry': 55})

    def test_other_http_error_returns_none(self):
        err = usage.urllib.error.HTTPError('u', 500, 'boom', {}, None)
        with patch.object(usage, 'claude_token', return_value='tok'), \
                patch.object(usage.urllib.request, 'urlopen', side_effect=err):
            self.assertIsNone(usage.claude_limits({}))


class ClaudeReadingTests(unittest.TestCase):
    def test_wraps_windows(self):
        with patch.object(usage, 'claude_limits', return_value=[{'minutes': 300, 'used': 1.0}]):
            self.assertEqual(usage.claude_reading({}), {'windows': [{'minutes': 300, 'used': 1.0}]})

    def test_passes_through_none_and_error(self):
        with patch.object(usage, 'claude_limits', return_value=None):
            self.assertIsNone(usage.claude_reading({}))
        with patch.object(usage, 'claude_limits', return_value={'error': 'rate'}):
            self.assertEqual(usage.claude_reading({}), {'error': 'rate'})

    def test_codex_reading(self):
        with patch.object(usage, 'codex_status', return_value={'email': 'x'}):
            self.assertEqual(usage.codex_reading(), {'email': 'x'})
        with patch.object(usage, 'codex_status', return_value=None):
            self.assertIsNone(usage.codex_reading())


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(usage, 'CACHE_FILE', Path(self.tmp.name) / 'cache.json')
        p.start()
        self.addCleanup(p.stop)

    def test_thresholds_from_routing(self):
        with patch.object(usage.config_tool, 'profiles',
                          return_value={'routing': {'usage_skip_percent': 90, 'usage_alert_percent': 70}}):
            self.assertEqual(usage.thresholds(), (90.0, 70.0))

    def test_store_and_load(self):
        usage.store('codex:main', {'email': 'x'})
        self.assertIn('codex:main', usage.load_cache()['readings'])

    def test_read_backoff_blocks_new_fetch(self):
        usage.read('claude:a', lambda: {'windows': []})
        calls = []
        refuse = lambda: calls.append(1) or {'error': 'rate', 'retry': 90}
        usage.read('claude:a', refuse, max_age=0)  # sets backoff
        usage.read('claude:a', refuse, max_age=0)  # inside backoff, not fetched
        self.assertEqual(len(calls), 1)

    def test_read_returns_none_without_cache_on_failure(self):
        self.assertIsNone(usage.read('codex:new', lambda: None, max_age=0))

    def test_alerts_include_reset_text(self):
        reading = {'windows': [{'minutes': 300, 'used': 90.0, 'resets': time.time() + 7200, 'label': None}]}
        with patch.object(usage, 'thresholds', return_value=(100.0, 80.0)):
            lines = usage.alerts('claude:a', reading, 'Claude a')
        self.assertEqual(len(lines), 1)
        self.assertIn('reseta em', lines[0])


class SnapshotTests(unittest.TestCase):
    def test_runs_one_job_per_account(self):
        with patch.object(usage, 'claude_account', return_value={'email': 'a@b', 'plan': 'Max'}), \
                patch.object(usage, 'read', return_value={'windows': []}), \
                patch.object(usage, 'openrouter_credits', return_value={'used': 0.0, 'limit': None, 'free': True}):
            result = usage.snapshot([('primary', {})], [('assinatura', None)], [('main', 'sk-or-x', None)])
        self.assertEqual(result[('claude-who', 'primary')], {'email': 'a@b', 'plan': 'Max'})
        self.assertIn(('codex', 'assinatura'), result)
        self.assertIn(('openrouter', 'main'), result)

    def test_codex_accounts_lists_subscription_plus_extras(self):
        with patch.object(usage.config_tool, 'tool_accounts', return_value=[('work', {'config_dir': '/tmp/w'})]), \
                patch.object(usage.config_tool, 'tool_env', return_value={'CODEX_HOME': '/tmp/w'}):
            found = usage.codex_accounts()
        self.assertEqual(found[0], ('assinatura', None))
        self.assertEqual(found[1][0], 'work')


if __name__ == '__main__':
    unittest.main()
