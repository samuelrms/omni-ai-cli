"""A body that parses as JSON but makes no sense gets an error answer, never a dropped connection."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import http.client
import json
import logging
import threading
import unittest
from unittest.mock import patch

from omni_ai.server import auth, handler, protocol
from omni_ai.server.protocol import RequestError

USER = {'role': 'user', 'content': 'hi'}
TOOLS = [{'type': 'function', 'function': {'name': 'f', 'parameters': {}}}]
MALFORMED = {
    'role is a list': '{"model":"omni/codex","messages":[{"role":[],"content":"hi"}]}',
    'role is an object': '{"model":"omni/codex","messages":[{"role":{},"content":"hi"}]}',
    'role with tools': '{"model":"omni/codex","tools":%s,"messages":[{"role":[],"content":"hi"}]}' % json.dumps(TOOLS),
    'tool_choice name': '{"model":"omni/codex","tools":%s,"tool_choice":{"type":"function","function":{"name":[]}},"messages":[{"role":"user","content":"hi"}]}' % json.dumps(TOOLS),
    'lone surrogate': '{"model":"omni/codex","messages":[{"role":"user","content":"\\ud800"}]}',
    'lone surrogate in a part': '{"model":"omni/codex","messages":[{"role":"user","content":[{"type":"text","text":"\\ud800"}]}]}',
    'lone surrogate in the model': '{"model":"omni/\\ud800","messages":[{"role":"user","content":"hi"}]}',
    'NUL in the content': '{"model":"omni/codex","messages":[{"role":"user","content":"a\\u0000b"}]}',
    'NUL with tools': '{"model":"omni/codex","tools":%s,"messages":[{"role":"user","content":"a\\u0000b"}]}' % json.dumps(TOOLS),
    'temperature with 400 digits': '{"model":"omni/codex","temperature":1%s,"messages":[{"role":"user","content":"hi"}]}' % ('0' * 400),
    'temperature infinity': '{"model":"omni/codex","temperature":Infinity,"messages":[{"role":"user","content":"hi"}]}',
    'temperature NaN': '{"model":"omni/codex","temperature":NaN,"messages":[{"role":"user","content":"hi"}]}',
    'nesting too deep': '[' * 200000,
}


class Parsing(unittest.TestCase):
    def test_valid_requests_still_pass(self):
        for body in ({'model': 'm', 'messages': [USER]},
                     {'model': 'm', 'messages': [USER], 'temperature': 0.5},
                     {'model': 'm', 'messages': [USER], 'temperature': 2},
                     {'model': 'm', 'messages': [{'role': 'user', 'content': 'emoji \U0001f600 and \\u0000 as plain text'}]},
                     {'model': 'm', 'messages': [USER], 'tools': TOOLS, 'tool_choice': {'type': 'function', 'function': {'name': 'f'}}}):
            with self.subTest(body=body):
                self.assertEqual(protocol.parse_request(body)['model'], 'm')

    def test_temperature_outside_the_range(self):
        for value in (-1, 2.1, 10 ** 400, float('inf'), float('nan'), True, '1'):
            with self.subTest(value=value), self.assertRaises(RequestError) as caught:
                protocol.parse_request({'model': 'm', 'messages': [USER], 'temperature': value})
            self.assertEqual(caught.exception.param, 'temperature')


class Answers(unittest.TestCase):
    def setUp(self):
        self.server = handler.Server(('127.0.0.1', 0))
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patcher = patch.object(auth, 'verify', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, body):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=20)
        self.addCleanup(client.close)
        client.request('POST', '/v1/chat/completions', body, {'Authorization': 'Bearer k', 'Content-Type': 'application/json'})
        response = client.getresponse()
        return response, json.loads(response.read())

    def test_each_one_gets_a_400_with_the_usual_format(self):
        for name, body in MALFORMED.items():
            with self.subTest(name=name):
                response, answer = self.post(body)
                self.assertEqual(response.status, 400)
                self.assertEqual(answer['error']['type'], 'invalid_request_error')

    def test_an_unexpected_failure_is_a_500_and_keeps_the_details_out(self):
        secret = 'text from the request'
        with patch.object(handler.protocol, 'parse_request', side_effect=KeyError(secret)), \
                self.assertLogs(handler.REQUEST_LOG, logging.ERROR) as logs:
            response, answer = self.post('{}')
        self.assertEqual(response.status, 500)
        self.assertEqual(answer['error']['type'], 'api_error')
        self.assertNotIn(secret, json.dumps(answer))
        self.assertIn('KeyError', logs.output[0])
        self.assertNotIn(secret, logs.output[0])

    def test_the_connection_is_closed_and_the_server_keeps_working(self):
        with patch.object(handler.protocol, 'parse_request', side_effect=KeyError('x')), self.assertLogs(handler.REQUEST_LOG, logging.ERROR):
            response, _ = self.post('{}')
        self.assertEqual(response.getheader('Connection'), 'close')
        self.assertEqual(self.post(MALFORMED['role is a list'])[0].status, 400)
        self.assertEqual(self.server.slots.acquire(blocking=False), True)
        self.server.slots.release()

    def test_a_failure_after_the_answer_started_writes_nothing_more(self):
        def half_answer(this, request, tool):
            this.send_response(200)
            this.send_header('Content-Length', '100')
            this.end_headers()
            this.wfile.write(b'partial')
            raise KeyError('x')
        with patch.object(handler.Handler, '_complete', half_answer), patch.object(handler, 'tool_of', return_value='codex'), \
                self.assertLogs(handler.REQUEST_LOG, logging.ERROR):
            client = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=20)
            self.addCleanup(client.close)
            client.request('POST', '/v1/chat/completions', json.dumps({'model': 'omni/codex', 'messages': [USER]}), {'Authorization': 'Bearer k'})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            with self.assertRaises(http.client.IncompleteRead) as caught:
                response.read()
            self.assertEqual(caught.exception.partial, b'partial')


if __name__ == '__main__':
    unittest.main()
