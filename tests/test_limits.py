import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONPATH'] = str(ROOT)
from omni_ai import cli as smart_ai  # noqa: E402


def stream(*events):
    return '\n'.join(json.dumps(e) for e in events)


class LimitDetectionTests(unittest.TestCase):
    @patch.dict(os.environ, SMART_AI_EVENTS='1')
    def test_tool_output_mentioning_limits_is_not_a_rate_limit(self):
        review = stream(
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'add rate limit: 20/day, HTTP 429 quota'}]}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'O teto de 429 requisições...'}]}},
            {'type': 'result', 'is_error': False, 'result': 'Review published; rate limit plan ok'})
        self.assertFalse(smart_ai.hit_limit(review, 0))
        self.assertFalse(smart_ai.hit_limit(review, 1))

    @patch.dict(os.environ, SMART_AI_EVENTS='1')
    def test_real_limits_are_detected(self):
        session = stream({'type': 'result', 'is_error': True, 'result': "You've hit your session limit · resets 7:40pm"})
        self.assertTrue(smart_ai.hit_limit(session, 1))
        rejected = stream({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}})
        self.assertTrue(smart_ai.hit_limit(rejected, 1))
        allowed = stream({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed'}})
        self.assertFalse(smart_ai.hit_limit(allowed, 0))
        other = stream({'type': 'result', 'is_error': True, 'result': 'Tool permission denied'})
        self.assertFalse(smart_ai.hit_limit(other, 1))

    @patch.dict(os.environ, SMART_AI_EVENTS='0')
    def test_text_mode_only_reads_tail_of_failed_run(self):
        body = 'line about rate limit in a file\n' + 'ok\n' * 10
        self.assertFalse(smart_ai.hit_limit(body, 0))
        self.assertTrue(smart_ai.hit_limit('...\nAPI Error: usage limit reached', 1))


if __name__ == '__main__':
    unittest.main()


class RememberLimitTests(unittest.TestCase):
    def test_keeps_tool_words_and_clears_on_success(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d, patch.object(smart_ai, 'STATE_FILE', Path(d) / 's.json'):
            smart_ai.remember_limit('codex', 'x\n■ You’ve hit your usage limit. try again at 9:20 PM.\ny')
            self.assertIn('9:20 PM', smart_ai.state()['limits']['codex']['text'])
            smart_ai.remember_limit('codex', '')
            self.assertNotIn('codex', smart_ai.state()['limits'])

    def test_json_error_keeps_only_the_message(self):
        import tempfile
        from pathlib import Path
        raw = '{"type":"error","message":"You\'ve hit your usage limit. Upgrade to Pro, or try again at 9:20 PM."}'
        with tempfile.TemporaryDirectory() as d, patch.object(smart_ai, 'STATE_FILE', Path(d) / 's.json'):
            smart_ai.remember_limit('codex', raw)
            text = smart_ai.state()['limits']['codex']['text']
            self.assertTrue(text.startswith("You've hit your usage limit"))
            self.assertNotIn('"type"', text)


class LimitExpiryTests(unittest.TestCase):
    def test_relative_reset_time(self):
        until = smart_ai.limit_until('Individual quota reached. Resets in 18h48m33s.', 1000)
        self.assertEqual(until, 1000 + 18 * 3600 + 48 * 60 + 33)

    def test_clock_reset_time_is_the_next_occurrence(self):
        import time
        seen = int(time.mktime((2026, 10, 26, 22, 0, 0, 0, 0, -1)))
        until = smart_ai.limit_until('try again at 9:20 PM.', seen)  # 21:20 already passed → next day
        self.assertEqual(time.localtime(until)[2:5], (27, 21, 20))
        until = smart_ai.limit_until("You've hit your session limit · resets 11:40pm", seen)
        self.assertEqual(time.localtime(until)[2:5], (26, 23, 40))

    def test_expired_limits_stop_showing(self):
        item = {'text': 'quota reached. Resets in 1h', 'at': 0}
        self.assertIsNotNone(smart_ai.active_limit(item, now=3599))
        self.assertIsNone(smart_ai.active_limit(item, now=3600))

    def test_limit_without_time_expires_after_a_day(self):
        item = {'text': 'rate limit exceeded', 'at': 0}
        self.assertIsNone(smart_ai.limit_until(item['text'], 0))
        self.assertIsNotNone(smart_ai.active_limit(item, now=smart_ai.LIMIT_FALLBACK - 1))
        self.assertIsNone(smart_ai.active_limit(item, now=smart_ai.LIMIT_FALLBACK))
