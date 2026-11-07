"""A stream always ends the way clients expect, also when the provider fails halfway."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import io
import json
import unittest

from omni_ai.server import handler
from omni_ai.server.execution import OK, SWITCHED, TIMEOUT


class FakeExecution:
    provider, tool = 'codex', 'codex'


class FakeHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        pass

    def end_headers(self):
        pass


def stream():
    request = {'model': 'omni/codex', 'include_usage': False, 'ignored': [], 'temperature': None}
    sender = FakeHandler()
    return handler.Stream(sender, request, FakeExecution()), sender


def events(sender):
    return [line[6:] for line in sender.wfile.getvalue().decode().split('\n') if line.startswith('data: ')]


USAGE = {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}


class Ends(unittest.TestCase):
    def test_a_failure_after_the_first_chunk_sends_the_error_and_then_done(self):
        for outcome in (SWITCHED, TIMEOUT, 'failed'):
            with self.subTest(outcome=outcome):
                item, sender = stream()
                item.delta('part')
                item.finish(outcome, '', 'stop', USAGE, 'estimated', None)
                sent = events(sender)
                self.assertEqual(sent[-1], '[DONE]')
                self.assertIn('error', json.loads(sent[-2]))

    def test_a_failure_before_the_first_chunk_is_still_an_http_error(self):
        item, sender = stream()
        with self.assertRaises(handler.RequestError):
            item.finish(SWITCHED, '', 'stop', USAGE, 'estimated', None)
        self.assertEqual(sender.wfile.getvalue(), b'')

    def test_a_good_stream_sends_done_once(self):
        item, sender = stream()
        item.delta('all')
        item.finish(OK, 'all', 'stop', USAGE, 'estimated', None)
        self.assertEqual(events(sender).count('[DONE]'), 1)


if __name__ == '__main__':
    unittest.main()
