import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import smart_ai  # noqa: E402


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
