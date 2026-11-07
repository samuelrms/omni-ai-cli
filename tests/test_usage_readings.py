"""Readings of usage and limits say what the tool said, and arrive when the tool has answered."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from omni_ai.cli import base, limits
from omni_ai.usage import providers

AT = int(time.mktime((2026, 11, 6, 12, 0, 0, 0, 0, -1)))


class ResetTimes(unittest.TestCase):
    def after(self, text):
        return limits.limit_until(text, AT) - AT

    def test_durations_in_any_form(self):
        for text, seconds in (('resets in 2h', 7200), ('resets in 2h30m', 9000), ('Resets in 18h48m33s.', 67713), ('resets in 2 hours 30 minutes', 9000), ('try again in 1 hour and 5 mins', 3900),
                              ('resets in 45 min', 2700), ('resets in 30s', 30), ('resets in 3 days', 259200), ('resets in 1 week', 604800),
                              ('resets in 1 month', 2592000), ('resets in 2d 3h', 183600), ('resets in 90 seconds', 90)):
            with self.subTest(text=text):
                self.assertEqual(self.after(text), seconds)

    def test_a_time_today_or_tomorrow(self):
        self.assertEqual(self.after('resets at 3pm'), 3 * 3600)
        self.assertEqual(self.after('resets 11am'), 23 * 3600)

    def test_a_date(self):
        moment = time.localtime(limits.limit_until('resets Nov 7 at 3pm', AT))
        self.assertEqual((moment.tm_mon, moment.tm_mday, moment.tm_hour), (11, 7, 15))
        moment = time.localtime(limits.limit_until('resets December 25, 10:30am', AT))
        self.assertEqual((moment.tm_mon, moment.tm_mday, moment.tm_hour, moment.tm_min), (12, 25, 10, 30))

    def test_a_date_already_past_is_next_year(self):
        moment = time.localtime(limits.limit_until('resets Jan 2 at 3pm', AT))
        self.assertEqual((moment.tm_year, moment.tm_mon), (2027, 1))

    def test_a_time_zone_counts(self):
        utc = limits.limit_until('resets 3pm (UTC)', AT)
        with_zone = limits.limit_until('resets 3pm (America/Sao_Paulo)', AT)
        self.assertEqual(time.gmtime(utc).tm_hour, 15)
        self.assertEqual(time.gmtime(with_zone).tm_hour, 18)

    def test_an_unknown_zone_falls_back_to_local_time(self):
        self.assertEqual(self.after('resets 3pm (Mars/Olympus)'), 3 * 3600)

    def test_no_time_in_the_text(self):
        self.assertIsNone(limits.limit_until('you hit your usage limit', AT))


class RememberedText(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / 'state.json'
        patcher = patch.object(limits, 'STATE_FILE', self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stored(self):
        return json.loads(self.state.read_text())['limits'].get('k', {}).get('text')

    def test_tool_output_mentioning_a_limit_is_not_the_message(self):
        output = '\n'.join((
            json.dumps({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'docs: the rate limit is 10 per second'}]}}),
            json.dumps({'type': 'assistant', 'message': {'content': 'about rate limit handling'}}),
            json.dumps({'type': 'result', 'is_error': True, 'result': "You've hit your usage limit, resets 7pm"}),
        ))
        limits.remember_limit('k', output)
        self.assertEqual(self.stored(), "You've hit your usage limit, resets 7pm")

    def test_plain_error_lines_at_the_end_count(self):
        limits.remember_limit('k', 'working\nworking\nError: rate limit exceeded, try again in 5 minutes\n')
        self.assertIn('rate limit exceeded', self.stored())

    def test_an_old_plain_line_far_from_the_end_does_not(self):
        limits.remember_limit('k', 'the rate limit doc says 10\n' + 'line\n' * 20)
        self.assertNotIn('k', json.loads(self.state.read_text())['limits'])

    def test_a_json_error_keeps_only_its_message(self):
        limits.remember_limit('k', json.dumps({'type': 'error', 'error': {'message': 'quota reached, resets in 1h'}}))
        self.assertEqual(self.stored(), 'quota reached, resets in 1h')

    def test_a_rejected_event_is_used(self):
        event = {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected', 'message': 'hit your session limit'}}
        limits.remember_limit('k', json.dumps(event))
        self.assertEqual(self.stored(), 'hit your session limit')

    def test_nothing_clears_what_was_stored(self):
        limits.remember_limit('k', 'Error: quota reached\n')
        limits.remember_limit('k', '')
        self.assertNotIn('k', json.loads(self.state.read_text())['limits'])


@unittest.skipIf(os.name == 'nt', 'POSIX script')
class CodexStatus(unittest.TestCase):
    """A fake app-server that answers both questions in a single write, as real buffering can."""

    def run_with(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'codex'
        path.write_text(f'#!{sys.executable}\nimport json, sys, time\n{body}\n')
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        with patch.object(providers, 'resolve_bin', return_value=str(path)):
            started = time.monotonic()
            return providers.codex_status(timeout=8), time.monotonic() - started

    TOGETHER = ("sys.stdout.write(json.dumps({'id': 2, 'result': {'account': {'email': 'a@b', 'planType': 'pro'}}}) + '\\n' + "
                "json.dumps({'id': 3, 'result': {'rateLimits': {'primary': {'usedPercent': 100, 'windowDurationMins': 300}, "
                "'rateLimitReachedType': 'x'}}}) + '\\n'); sys.stdout.flush(); time.sleep(30)")

    def test_two_answers_in_one_write_are_both_read(self):
        status, took = self.run_with(self.TOGETHER)
        self.assertEqual(status['windows'][0]['used'], 100)
        self.assertTrue(status['blocked'])
        self.assertLess(took, 4)

    def test_a_server_that_says_nothing_gives_up_on_time(self):
        status, took = self.run_with('time.sleep(30)')
        self.assertIsNone(status)
        self.assertLess(took, 12)

    def test_noise_between_answers_is_skipped(self):
        status, _ = self.run_with("print('not json'); print('[]'); " + self.TOGETHER)
        self.assertEqual(status['windows'][0]['used'], 100)


if __name__ == '__main__':
    unittest.main()
