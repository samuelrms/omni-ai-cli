import isolation  # noqa: F401  (first: keeps the tests away from real data)
from http.server import BaseHTTPRequestHandler, HTTPServer
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omni_ai import config  # noqa: E402
from omni_ai.server import auth, handler, native, protocol  # noqa: E402

WEATHER = {'type': 'function', 'function': {'name': 'weather', 'parameters': {
    'type': 'object', 'required': ['city'], 'additionalProperties': False,
    'properties': {'city': {'type': 'string'}, 'days': {'type': 'integer'}, 'unit': {'enum': ['c', 'f']}}}}}
USER = [{'role': 'user', 'content': 'weather in Lisbon?'}]


def call(arguments='{"city": "Lisbon"}', name='weather'):
    return {'id': 'call_1', 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


def reply(calls=None, content=None, finish='tool_calls'):
    return {'model': 'fake', 'choices': [{'message': {'role': 'assistant', 'content': content, 'tool_calls': calls}, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 3}}


class Upstream(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.seen.append((self.path, self.headers['Authorization'], body))
        status, answer = self.server.script.pop(0) if self.server.script else (500, {})
        data = json.dumps(answer).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class Base(unittest.TestCase):
    keys = {'OPENROUTER_API_KEY': 'sk-or-one', 'DEEPSEEK_API_KEY': 'sk-two'}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.upstream = HTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream.seen, self.upstream.script = [], []
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)
        base = f'http://127.0.0.1:{self.upstream.server_port}'
        env = {**self.keys, auth.ENV_KEY: 'client', 'SMART_AI_STATE': tmp.name + '/state', 'SMART_AI_LOG': tmp.name + '/log'}
        state = Path(tmp.name) / 'state'
        for item in (patch.dict(os.environ, env), patch.dict(native.BASES, {'openrouter': base, 'deepseek': base + '/ds', 'grok': base + '/x'}),
                     patch('omni_ai.cli.base.STATE_FILE', state), patch.object(config, 'KEYS_DIR', Path(tmp.name) / 'k'),
                     patch.object(config.keys, 'KEYS_DIR', Path(tmp.name) / 'k'), patch.object(config.keys, 'LEGACY_OPENROUTER', Path(tmp.name) / 'l'),
                     patch.object(handler, 'available', lambda name: True), patch('omni_ai.cli.base.say', lambda *a: None)):
            item.start()
            self.addCleanup(item.stop)
        self.server = handler.Server(('127.0.0.1', 0))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, **fields):
        body = {'model': 'omni-auto', 'messages': USER, 'tools': [WEATHER], **fields}
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=20)
        self.addCleanup(connection.close)
        connection.request('POST', '/v1/chat/completions', json.dumps(body), {'Authorization': 'Bearer client'})
        response = connection.getresponse()
        text = response.read().decode()
        return response, (text if fields.get('stream') and response.status == 200 else json.loads(text))


class Calls(Base):
    def test_tool_call_reaches_the_client(self):
        self.upstream.script = [(200, reply([call()]))]
        response, body = self.post()
        choice = body['choices'][0]
        self.assertEqual((response.status, choice['finish_reason']), (200, 'tool_calls'))
        self.assertIsNone(choice['message']['content'])
        self.assertEqual(choice['message']['tool_calls'][0]['function']['name'], 'weather')
        self.assertEqual(response.headers['X-Omni-Provider'], 'openrouter')
        self.assertEqual(body['usage']['prompt_tokens'], 5)

    def test_what_goes_to_the_provider(self):
        self.upstream.script = [(200, reply([call()]))]
        self.post(tool_choice='required', parallel_tool_calls=False, temperature=0.5, max_tokens=50)
        path, authorization, sent = self.upstream.seen[0]
        self.assertEqual((path, authorization), ('/chat/completions', 'Bearer sk-or-one'))
        self.assertEqual(sent['tools'], [WEATHER])
        self.assertEqual((sent['tool_choice'], sent['parallel_tool_calls'], sent['temperature'], sent['max_tokens']), ('required', False, 0.5, 50))
        self.assertEqual(sent['provider'], {'require_parameters': True})
        self.assertEqual(sent['model'], 'openrouter/free')

    def test_plain_answer_with_tools_available(self):
        self.upstream.script = [(200, reply(None, 'No need for a tool', 'stop'))]
        _, body = self.post()
        self.assertEqual(body['choices'][0]['message'], {'role': 'assistant', 'content': 'No need for a tool'})
        self.assertEqual(body['choices'][0]['finish_reason'], 'stop')

    def test_conversation_with_tool_result(self):
        self.upstream.script = [(200, reply(None, 'It is 20 degrees', 'stop'))]
        messages = USER + [{'role': 'assistant', 'content': None, 'tool_calls': [call()]},
                           {'role': 'tool', 'tool_call_id': 'call_1', 'content': '20'}]
        response, _ = self.post(messages=messages)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.upstream.seen[0][2]['messages'][2]['tool_call_id'], 'call_1')

    def test_streaming_format(self):
        self.upstream.script = [(200, reply([call()]))]
        response, text = self.post(stream=True, stream_options={'include_usage': True})
        events = [line[6:] for line in text.split('\n') if line.startswith('data: ')]
        self.assertEqual(events[-1], '[DONE]')
        chunks = [json.loads(e) for e in events[:-1]]
        deltas = [c['choices'][0] for c in chunks if c['choices']]
        piece = next(d['delta']['tool_calls'][0] for d in deltas if 'tool_calls' in d['delta'])
        self.assertEqual((piece['index'], piece['id'], piece['function']['arguments']), (0, 'call_1', '{"city": "Lisbon"}'))
        self.assertEqual(deltas[-1]['finish_reason'], 'tool_calls')
        self.assertEqual(chunks[-1]['usage']['completion_tokens'], 3)

    def test_personal_data_is_masked_both_ways(self):
        secret = 'ana.silva@example.com'
        echoed = {}

        def answer():
            sent = json.dumps(self.upstream.seen)
            echoed['leak'] = secret in sent
        self.upstream.script = [(200, reply([call()]))]
        messages = [{'role': 'user', 'content': f'mail {secret}'}, {'role': 'assistant', 'content': None, 'tool_calls': [call(f'{{"city": "{secret}"}}')]},
                    {'role': 'tool', 'tool_call_id': 'call_1', 'content': secret}]
        self.post(messages=messages)
        answer()
        self.assertFalse(echoed['leak'])
        sent = self.upstream.seen[0][2]['messages'][0]['content']
        placeholder = sent.split('mail ')[1]
        self.upstream.script = [(200, reply([call(f'{{"city": "{placeholder}"}}')]))]
        with patch.object(native.Masking, '_mask', lambda s, text: (s.mapping.update({placeholder: secret}), text.replace(secret, placeholder))[1]):
            _, body = self.post(messages=messages)
        self.assertIn(secret, body['choices'][0]['message']['tool_calls'][0]['function']['arguments'])


class Fallback(Base):
    def assert_second_provider_used(self):
        response, body = self.post()
        self.assertEqual((response.status, response.headers['X-Omni-Provider']), (200, 'deepseek'))
        self.assertEqual(self.upstream.seen[1][0], '/ds/chat/completions')

    def test_invalid_json_arguments(self):
        self.upstream.script = [(200, reply([call('{not json')])), (200, reply([call()]))]
        self.assert_second_provider_used()

    def test_arguments_outside_the_schema(self):
        self.upstream.script = [(200, reply([call('{"days": 2}')])), (200, reply([call()]))]
        self.assert_second_provider_used()

    def test_unknown_function(self):
        self.upstream.script = [(200, reply([call(name='rm_rf')])), (200, reply([call()]))]
        self.assert_second_provider_used()

    def test_provider_error(self):
        self.upstream.script = [(500, {}), (200, reply([call()]))]
        self.assert_second_provider_used()

    def test_empty_answer(self):
        self.upstream.script = [(200, reply(None, '', 'stop')), (200, reply([call()]))]
        self.assert_second_provider_used()

    def test_required_without_a_call(self):
        self.upstream.script = [(200, reply(None, 'no', 'stop')), (200, reply(None, 'no', 'stop'))]
        response, body = self.post(tool_choice='required')
        self.assertEqual(response.status, 502)

    def test_all_failed_says_nothing_of_the_provider(self):
        self.upstream.script = [(400, {'error': 'upstream secret'}), (400, {})]
        response, body = self.post()
        self.assertEqual(response.status, 502)
        self.assertNotIn('upstream secret', json.dumps(body))

    def test_all_limited(self):
        self.upstream.script = [(429, {}), (429, {})]
        self.assertEqual(self.post()[0].status, 429)
        self.assertEqual(len(self.upstream.seen), 2)
        self.assertEqual(self.post()[0].status, 502)  # both accounts are cooling down: nobody is called
        self.assertEqual(len(self.upstream.seen), 2)

    def test_paid_model_stays_blocked(self):
        with patch.object(config, 'model_values', lambda: {'openrouter': 'openai/gpt-x', 'openrouter_paid': 'off', 'deepseek': '', 'grok': ''}):
            self.upstream.script = [(200, reply([call()]))]
            self.assertEqual(self.post()[0].headers['X-Omni-Provider'], 'deepseek')


class Refused(Base):
    def test_agent_cli(self):
        response, body = self.post(model='omni/claude')
        self.assertEqual((response.status, body['error']['code']), (400, 'tools_not_supported'))
        self.assertEqual(self.upstream.seen, [])

    def test_no_keys(self):
        with patch.object(config, 'stored_keys', lambda provider: []):
            self.assertEqual(self.post()[0].status, 400)

    def test_never_runs_anything(self):
        with patch('subprocess.Popen', side_effect=AssertionError('nothing may run')):
            self.upstream.script = [(200, reply([call()]))]
            self.assertEqual(self.post()[0].status, 200)

    def test_disconnect_stops_waiting(self):
        run = native.Native('auto', protocol.parse_request({'model': 'omni-auto', 'messages': USER, 'tools': [WEATHER]}))
        run.stop('timeout')
        self.assertEqual(run.run(), 'timeout')


class Validation(unittest.TestCase):
    def parse(self, **fields):
        return protocol.parse_request({'model': 'm', 'messages': USER, 'tools': [WEATHER], **fields})

    def test_bad_requests(self):
        for fields in ({'tools': 'x'}, {'tools': [{'type': 'web_search'}]}, {'tools': [{'type': 'function', 'function': {'name': 'a b'}}]},
                       {'tools': [WEATHER, WEATHER]}, {'tools': [{'type': 'function', 'function': {'name': 'a', 'parameters': 1}}]},
                       {'tools': [WEATHER] * 200}, {'tool_choice': 'maybe'}, {'tool_choice': {'type': 'function', 'function': {'name': 'ghost'}}},
                       {'parallel_tool_calls': 'yes'}, {'messages': [{'role': 'tool', 'content': 'x'}]},
                       {'messages': [{'role': 'user', 'content': 'x', 'tool_calls': []}]}, {'messages': []},
                       {'messages': [{'role': 'assistant', 'tool_calls': [{'function': {}}]}]}, {'functions': [{'name': 'a'}]}):
            with self.assertRaises(protocol.RequestError, msg=fields) as caught:
                self.parse(**fields)
            self.assertEqual(caught.exception.status, 400)

    def test_tool_message_without_tools_is_still_refused(self):
        with self.assertRaises(protocol.RequestError):
            protocol.parse_request({'model': 'm', 'messages': [{'role': 'tool', 'tool_call_id': 'x', 'content': 'y'}]})

    def test_empty_tools_is_a_plain_request(self):
        self.assertIsNone(self.parse(tools=[])['tools'])

    def test_schema(self):
        schema = WEATHER['function']['parameters']
        self.assertTrue(native.fits_schema({'city': 'x', 'days': 2, 'unit': 'c'}, schema))
        for bad in ({}, {'city': 1}, {'city': 'x', 'days': 'two'}, {'city': 'x', 'days': True}, {'city': 'x', 'unit': 'k'}, {'city': 'x', 'other': 1}):
            self.assertFalse(native.fits_schema(bad, schema), bad)
        self.assertTrue(native.fits_schema({'a': [1, 2]}, {'properties': {'a': {'type': 'array', 'items': {'type': 'number'}}}}))
        self.assertFalse(native.fits_schema({'a': ['x']}, {'properties': {'a': {'type': 'array', 'items': {'type': 'number'}}}}))
        self.assertTrue(native.fits_schema({'a': 1}, {'properties': {'a': {'type': ['string', 'number']}}}))
        self.assertTrue(native.fits_schema({'a': 1}, None))


if __name__ == '__main__':
    unittest.main()
