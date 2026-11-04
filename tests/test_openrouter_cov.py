import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)
sys.path.insert(0, str(ROOT))
from omni_ai import openrouter
from omni_ai.openrouter import OpenRouterClient, find_best_model, DEFAULT_MODEL


class FakeResp:
    def __init__(self, payload):
        self._data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._data


def http_error(code, body):
    err = urllib.error.HTTPError('http://x', code, 'err', {}, None)
    err.read = lambda: (body if isinstance(body, bytes) else json.dumps(body).encode())
    err.close = lambda: None
    return err


class RequestTests(unittest.TestCase):
    def test_get_request_success(self):
        client = OpenRouterClient(api_key='k')
        with patch('omni_ai.openrouter.urllib.request.urlopen', return_value=FakeResp({'ok': 1})):
            self.assertEqual(client._request('/models'), {'ok': 1})

    def test_post_request_builds_body_and_auth_header(self):
        client = OpenRouterClient(api_key='secret-key')
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['headers'] = req.headers
            captured['data'] = req.data
            return FakeResp({'choices': []})
        with patch('omni_ai.openrouter.urllib.request.urlopen', side_effect=fake_urlopen):
            client._request('/chat/completions', data={'a': 1}, method='POST')
        self.assertIn('Authorization', captured['headers'])
        self.assertEqual(json.loads(captured['data']), {'a': 1})

    def test_request_emits_event_on_stderr(self):
        client = OpenRouterClient(api_key='k')
        trace = io.StringIO()
        with patch.dict(os.environ, {'SMART_AI_EVENTS': '1'}), \
             patch('omni_ai.openrouter.urllib.request.urlopen', return_value=FakeResp({'ok': 1})), \
             contextlib.redirect_stderr(trace):
            client._request('/chat/completions', data={'messages': []}, method='POST')
        self.assertIn('smart.request', trace.getvalue())

    def test_http_error_with_parseable_reason(self):
        client = OpenRouterClient(api_key='k')
        err = http_error(429, {'error': {'message': 'Rate limited hard'}})
        with patch('omni_ai.openrouter.urllib.request.urlopen', side_effect=err):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 429: Rate limited hard'):
                client._request('/models')

    def test_http_error_with_unparseable_body(self):
        client = OpenRouterClient(api_key='k')
        err = http_error(500, b'<html>boom</html>')
        with patch('omni_ai.openrouter.urllib.request.urlopen', side_effect=err):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 500$'):
                client._request('/models')

    def test_value_error_becomes_invalid_response(self):
        client = OpenRouterClient(api_key='k')
        with patch('omni_ai.openrouter.urllib.request.urlopen', side_effect=ValueError('bad json')):
            with self.assertRaisesRegex(RuntimeError, 'inválida ou timeout'):
                client._request('/models')

    def test_url_error_becomes_network_failure(self):
        client = OpenRouterClient(api_key='k')
        with patch('omni_ai.openrouter.urllib.request.urlopen', side_effect=urllib.error.URLError('down')):
            with self.assertRaisesRegex(RuntimeError, 'Falha de rede'):
                client._request('/models')


class StreamRequestErrorTests(unittest.TestCase):
    def test_stream_emits_event(self):
        client = OpenRouterClient(api_key='k')

        class Resp:
            def __enter__(self): return iter([b'data: [DONE]\n'])
            def __exit__(self, *a): return False
        trace = io.StringIO()
        with patch.dict(os.environ, {'SMART_AI_EVENTS': '1'}), \
             patch('omni_ai.openrouter.urllib.request.urlopen', return_value=Resp()), \
             contextlib.redirect_stderr(trace):
            list(client._stream_request({'messages': []}))
        self.assertIn('smart.request', trace.getvalue())

    def test_stream_skips_malformed_json_chunks(self):
        client = OpenRouterClient(api_key='k')
        lines = [
            b'data: not-json\n',
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
            b'data: [DONE]\n',
        ]

        class Resp:
            def __enter__(self): return iter(lines)
            def __exit__(self, *a): return False
        with patch('omni_ai.openrouter.urllib.request.urlopen', return_value=Resp()):
            events = list(client._stream_request({'messages': []}))
        self.assertEqual(events[0], ('delta', 'hi'))
        self.assertEqual(events[-1][0], 'final')

    def test_stream_http_error_with_reason(self):
        client = OpenRouterClient(api_key='k')
        err = http_error(402, {'error': {'message': 'No credit'}})
        with patch('omni_ai.openrouter.urllib.request.urlopen', side_effect=err):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 402: No credit'):
                list(client._stream_request({'messages': []}))

    def test_stream_http_error_unparseable(self):
        client = OpenRouterClient(api_key='k')
        err = http_error(503, b'nope')
        with patch('omni_ai.openrouter.urllib.request.urlopen', side_effect=err):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 503$'):
                list(client._stream_request({'messages': []}))

    def test_stream_value_error(self):
        client = OpenRouterClient(api_key='k')
        with patch('omni_ai.openrouter.urllib.request.urlopen', side_effect=TimeoutError()):
            with self.assertRaisesRegex(RuntimeError, 'inválida ou timeout'):
                list(client._stream_request({'messages': []}))

    def test_stream_url_error(self):
        client = OpenRouterClient(api_key='k')
        with patch('omni_ai.openrouter.urllib.request.urlopen', side_effect=urllib.error.URLError('x')):
            with self.assertRaisesRegex(RuntimeError, 'Falha de rede'):
                list(client._stream_request({'messages': []}))


class ListFreeModelsTests(unittest.TestCase):
    def test_filters_only_fully_free_models(self):
        client = OpenRouterClient(api_key='k')
        catalog = {'data': [
            {'id': 'b/free', 'pricing': {'prompt': '0', 'completion': '0'}},
            {'id': 'a/free', 'pricing': {'prompt': '0', 'completion': '0'}},
            {'id': 'paid', 'pricing': {'prompt': '0.01', 'completion': '0'}},
            {'id': 'half', 'pricing': {'prompt': '0', 'completion': '0.5'}},
        ]}
        with patch.object(client, '_request', return_value=catalog):
            models = client.list_free_models()
        self.assertEqual([m['id'] for m in models], ['a/free', 'b/free'])


class ChatBranchTests(unittest.TestCase):
    def test_pii_shield_logged_and_system_masked(self):
        client = OpenRouterClient(api_key='k', fallback_models=['t/a:free'])
        trace = io.StringIO()
        with patch.object(client, '_request', return_value={'choices': [{'message': {'content': 'ok'}}]}), \
             contextlib.redirect_stderr(trace):
            client.chat('meu email alice@example.com', system_prompt='contato bob@example.com')
        self.assertIn('PII mascarado', trace.getvalue())

    def test_auto_model_list_builds_candidates_from_catalog(self):
        client = OpenRouterClient(api_key='k')  # fallback_models None -> queries catalog
        catalog = [{'id': 'google/gemma-4-31b-it:free'}, {'id': 'other/x:free'}, {'id': 'paid/nope'}]
        sent = []

        def fake_request(endpoint, data=None, method='GET'):
            sent.append(data['models'])
            return {'choices': [{'message': {'content': 'ok'}}], 'model': data['models'][0]}
        with patch.object(client, 'list_free_models', return_value=catalog), \
             patch.object(client, '_request', side_effect=fake_request):
            client.chat('oi')
        self.assertIn('google/gemma-4-31b-it:free', sent[0])

    def test_group_failure_prints_fallback_notice(self):
        client = OpenRouterClient(api_key='k', fallback_models=[f't/m{i}:free' for i in range(4)])
        trace = io.StringIO()

        def fake(endpoint, data=None, method='GET'):
            if 't/m0:free' in data['models']:
                raise RuntimeError('OpenRouter HTTP 429: busy')
            return {'choices': [{'message': {'content': 'ok'}}], 'model': 'x'}
        with patch.object(client, '_request', side_effect=fake), contextlib.redirect_stderr(trace):
            client.chat('oi')
        self.assertIn('indisponíveis', trace.getvalue())

    def test_empty_content_raises(self):
        client = OpenRouterClient(api_key='k', fallback_models=[])
        with patch.object(client, '_request', return_value={'choices': [{'message': {'content': '   '}}]}):
            with self.assertRaisesRegex(RuntimeError, 'vazio ou inválido'):
                client.chat('oi')

    def test_non_string_content_raises(self):
        client = OpenRouterClient(api_key='k', fallback_models=[])
        with patch.object(client, '_request', return_value={'choices': [{'message': {'content': {'x': 1}}}]}):
            with self.assertRaisesRegex(RuntimeError, 'vazio ou inválido'):
                client.chat('oi')

    def test_usage_and_model_returned(self):
        client = OpenRouterClient(api_key='k', fallback_models=['t/a:free'], enable_masking=False)
        resp = {'choices': [{'message': {'content': 'hello'}}], 'model': 't/a:free', 'usage': {'total_tokens': 9}}
        with patch.object(client, '_request', return_value=resp):
            answer = client.chat('oi')
        self.assertEqual(answer['model'], 't/a:free')
        self.assertEqual(answer['usage'], {'total_tokens': 9})
        self.assertEqual(answer['masked_pii_count'], 0)

    def test_streaming_events_env_suppresses_fallback_print(self):
        client = OpenRouterClient(api_key='k', fallback_models=['t/a:free', 't/b:free', 't/c:free', 't/d:free'])

        def fake_stream(data):
            if 't/a:free' in data['models']:
                yield ('final', {'content': '', 'model': None, 'usage': {}})
            else:
                yield ('delta', 'ok')
                yield ('final', {'content': 'ok', 'model': 't/d:free', 'usage': {}})
        trace = io.StringIO()
        with patch.dict(os.environ, {'SMART_AI_EVENTS': '1'}), \
             patch.object(client, '_stream_request', side_effect=fake_stream), \
             contextlib.redirect_stderr(trace):
            client.chat('oi', on_delta=lambda _p: None)
        self.assertNotIn('indisponíveis', trace.getvalue())


class FindBestModelTests(unittest.TestCase):
    def test_returns_preferred_when_available(self):
        client = OpenRouterClient(api_key='k')
        catalog = [{'id': 'google/gemma-4-31b-it:free'}, {'id': 'other'}]
        with patch.object(client, 'list_free_models', return_value=catalog):
            self.assertEqual(find_best_model(client), 'google/gemma-4-31b-it:free')

    def test_falls_back_to_default_on_error(self):
        client = OpenRouterClient(api_key='k')
        with patch.object(client, 'list_free_models', side_effect=RuntimeError('x')):
            self.assertEqual(find_best_model(client), DEFAULT_MODEL)

    def test_returns_default_when_no_preferred_available(self):
        client = OpenRouterClient(api_key='k')
        with patch.object(client, 'list_free_models', return_value=[{'id': 'unknown/model:free'}]):
            self.assertEqual(find_best_model(client), DEFAULT_MODEL)


class MainCliTests(unittest.TestCase):
    def run_main(self, argv, env=None, stdin='', stdin_tty=False):
        out, err = io.StringIO(), io.StringIO()
        environ = dict(os.environ)
        environ.pop('SMART_AI_EVENTS', None)
        environ.pop('SMART_AI_OPENROUTER_PAID', None)
        if env:
            environ.update(env)
        fake_stdin = io.StringIO(stdin)
        fake_stdin.isatty = lambda: stdin_tty
        with patch.object(sys, 'argv', ['openrouter'] + argv), \
             patch.dict(os.environ, environ, clear=True), \
             patch.object(sys, 'stdin', fake_stdin), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                openrouter.main()
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_missing_key_exits(self):
        code, _out, err = self.run_main(['hello'], env={})
        self.assertEqual(code, 1)
        self.assertIn('OPENROUTER_API_KEY', err)

    def test_list_models(self):
        with patch.object(OpenRouterClient, 'list_free_models',
                          return_value=[{'id': 'a/free', 'name': 'A', 'context_length': 8000}]):
            code, out, _err = self.run_main(['--list-models'], env={'OPENROUTER_API_KEY': 'k'})
        self.assertEqual(code, 0)
        self.assertIn('a/free', out)

    def test_paid_rejected_via_parser_error(self):
        code, _out, err = self.run_main(['--model', 'vendor/paid', 'hi'], env={'OPENROUTER_API_KEY': 'k'})
        self.assertEqual(code, 2)  # argparse error exit code
        self.assertIn('pago', err)

    def test_prompt_from_stdin_when_no_args(self):
        with patch.object(OpenRouterClient, 'chat', return_value={'content': 'resposta', 'model': 'm', 'usage': {}, 'masked_pii_count': 0}):
            code, out, _err = self.run_main(['--json'], env={'OPENROUTER_API_KEY': 'k'}, stdin='pergunta', stdin_tty=False)
        self.assertEqual(code, 0)
        self.assertIn('resposta', out)

    def test_no_prompt_and_tty_prints_help_and_exits(self):
        code, _out, _err = self.run_main([], env={'OPENROUTER_API_KEY': 'k'}, stdin='', stdin_tty=True)
        self.assertEqual(code, 1)

    def test_json_output(self):
        result = {'content': 'oi', 'model': 'm', 'usage': {}, 'masked_pii_count': 0}
        with patch.object(OpenRouterClient, 'chat', return_value=result):
            code, out, _err = self.run_main(['--json', 'hello'], env={'OPENROUTER_API_KEY': 'k'})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['content'], 'oi')

    def test_events_output(self):
        captured = {}

        def fake_chat(self, prompt, system_prompt=None, temperature=0.7, on_delta=None):
            captured['on_delta'] = on_delta
            on_delta('tok')
            return {'content': 'full', 'model': 'm', 'usage': {}}
        with patch.object(OpenRouterClient, 'chat', fake_chat):
            code, out, _err = self.run_main(['hi'], env={'OPENROUTER_API_KEY': 'k', 'SMART_AI_EVENTS': '1'})
        self.assertEqual(code, 0)
        self.assertIn('smart.delta', out)
        self.assertIn('smart.answer', out)

    def test_streaming_human_output(self):
        def fake_chat(self, prompt, system_prompt=None, temperature=0.7, on_delta=None):
            on_delta('streamed ')
            return {'content': 'streamed text', 'model': 'm', 'usage': {}}
        with patch.object(OpenRouterClient, 'chat', fake_chat):
            code, out, _err = self.run_main(['hi'], env={'OPENROUTER_API_KEY': 'k'})
        self.assertEqual(code, 0)
        self.assertIn('streamed', out)

    def test_runtime_error_exits_1(self):
        with patch.object(OpenRouterClient, 'chat', side_effect=RuntimeError('boom')):
            code, _out, err = self.run_main(['--json', 'hi'], env={'OPENROUTER_API_KEY': 'k'})
        self.assertEqual(code, 1)
        self.assertIn('boom', err)

    def test_keyboard_interrupt_exits_130(self):
        with patch.object(OpenRouterClient, 'chat', side_effect=KeyboardInterrupt()):
            code, _out, _err = self.run_main(['--json', 'hi'], env={'OPENROUTER_API_KEY': 'k'})
        self.assertEqual(code, 130)

    def test_allow_paid_via_env(self):
        result = {'content': 'oi', 'model': 'vendor/paid', 'usage': {}, 'masked_pii_count': 0}
        with patch.object(OpenRouterClient, 'chat', return_value=result):
            code, out, _err = self.run_main(['--model', 'vendor/paid', '--json', 'hi'],
                                            env={'OPENROUTER_API_KEY': 'k', 'SMART_AI_OPENROUTER_PAID': 'on'})
        self.assertEqual(code, 0)


if __name__ == '__main__':
    unittest.main()
