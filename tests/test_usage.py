from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omni_ai import usage  # noqa: E402


class UsageFormatTests(unittest.TestCase):
    def test_bar_is_clamped_and_proportional(self):
        self.assertEqual(usage.bar(0, 10), '░' * 10)
        self.assertEqual(usage.bar(50, 10), '█' * 5 + '░' * 5)
        self.assertEqual(usage.bar(140, 10), '█' * 10)

    def test_window_labels(self):
        self.assertEqual(usage.window_label(300), '5h')
        self.assertEqual(usage.window_label(10080), 'semana')
        self.assertEqual(usage.window_label(0), 'crédito')

    def test_resets_in(self):
        self.assertEqual(usage.resets_in(100, now=200), 'agora')
        self.assertEqual(usage.resets_in(1000 + 45 * 60, now=1000), '45m')
        self.assertEqual(usage.resets_in(3 * 3600 + 600, now=0), '3h10m')
        self.assertEqual(usage.resets_in(6 * 86400 + 3600, now=0), '6d01h')

    def test_plan_names(self):
        self.assertEqual(usage.plan_name('max'), 'Max')
        self.assertEqual(usage.plan_name(None), '')


class ClaudeWindowTests(unittest.TestCase):
    def test_parses_usage_payload(self):
        data = {'five_hour': {'utilization': 58.0, 'resets_at': '2026-10-27T18:00:00Z'},
                'seven_day': {'utilization': 16, 'resets_at': None}, 'seven_day_opus': None}
        windows = usage.claude_windows(data)
        self.assertEqual([w['minutes'] for w in windows], [300, 10080])
        self.assertEqual(windows[0]['used'], 58.0)
        self.assertIsNotNone(windows[0]['resets'])
        self.assertIsNone(windows[1]['resets'])


class CheckTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest.mock import patch
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(usage, 'CACHE_FILE', Path(self.tmp.name) / 'cache.json')
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_read_uses_cache_while_fresh(self):
        calls = []
        fetch = lambda: calls.append(1) or {'windows': [{'minutes': 300, 'used': 40.0, 'resets': None}]}
        usage.read('claude:primary', fetch)
        usage.read('claude:primary', fetch)
        self.assertEqual(len(calls), 1)
        usage.read('claude:primary', fetch, max_age=0)
        self.assertEqual(len(calls), 2)

    def test_peak_ignores_windows_already_reset(self):
        import time
        windows = [{'used': 100.0, 'resets': time.time() - 5}, {'used': 30.0, 'resets': time.time() + 600}]
        self.assertEqual(usage.peak(windows), 30.0)

    def test_alert_is_said_once_per_window(self):
        from unittest.mock import patch
        reading = {'windows': [{'minutes': 300, 'used': 85.0, 'resets': 2000000000}]}
        with patch.object(usage, 'thresholds', return_value=(100.0, 80.0)):
            self.assertEqual(len(usage.alerts('claude:primary', reading, 'Claude primary')), 1)
            self.assertEqual(usage.alerts('claude:primary', reading, 'Claude primary'), [])

    def test_refused_check_returns_last_reading_and_waits(self):
        calls = []
        ok = {'windows': [{'minutes': 300, 'used': 11.0, 'resets': None}]}
        usage.read('claude:primary', lambda: ok)
        refuse = lambda: calls.append(1) or {'error': 'rate', 'retry': 90}
        first = usage.read('claude:primary', refuse, max_age=0)
        self.assertTrue(first['stale'])
        self.assertEqual(first['windows'], ok['windows'])
        usage.read('claude:primary', refuse, max_age=0)  # still inside the wait: no new check
        self.assertEqual(len(calls), 1)
