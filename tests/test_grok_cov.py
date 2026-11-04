import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)
sys.path.insert(0, str(ROOT))
from omni_ai import grok


class FakeResp:
    """Context manager mimicking urlopen's return value."""
    def __init__(self, payload):
        self._data = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body):
    data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    return urllib.error.HTTPError('https://api.x.ai/v1/chat/completions', code, 'err', {}, io.BytesIO(data))


class RequestTests(unittest.TestCase):
    def test_get_success_no_body(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['method'] = req.get_method()
            captured['data'] = req.data
            return FakeResp({'data': [{'id': 'grok-1'}]})

        with patch('omni_ai.grok.urllib.request.urlopen', side_effect=fake_urlopen):
            out = grok.request('/models', 'synthetic-key')
        self.assertEqual(out, {'data': [{'id': 'grok-1'}]})
        self.assertEqual(captured['method'], 'GET')
        self.assertIsNone(captured['data'])

    def test_post_success_with_body(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured['method'] = req.get_method()
            captured['body'] = json.loads(req.data.decode())
            captured['auth'] = req.get_header('Authorization')
            return FakeResp({'ok': True})

        with patch('omni_ai.grok.urllib.request.urlopen', side_effect=fake_urlopen):
            out = grok.request('/chat/completions', 'synthetic-key', {'model': 'm'})
        self.assertEqual(out, {'ok': True})
        self.assertEqual(captured['method'], 'POST')
        self.assertEqual(captured['body'], {'model': 'm'})
        self.assertEqual(captured['auth'], 'Bearer synthetic-key')

    def test_http_error_dict_message_becomes_runtime_error(self):
        err = http_error(401, {'error': {'message': 'invalid key'}})
        with patch('omni_ai.grok.urllib.request.urlopen', side_effect=err):
            with self.assertRaisesRegex(RuntimeError, 'xAI HTTP 401: invalid key'):
                grok.request('/models', 'bad')

    def test_http_error_string_error_field(self):
        err = http_error(400, {'error': 'plain string error'})
        with patch('omni_ai.grok.urllib.request.urlopen', side_effect=err):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 400: plain string error'):
                grok.request('/models', 'k', name='xAI')

    def test_http_error_non_json_body_has_no_reason(self):
        err = http_error(500, b'<html>oops</html>')
        with patch('omni_ai.grok.urllib.request.urlopen', side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                grok.request('/models', 'k')
        self.assertEqual(str(ctx.exception), 'xAI HTTP 500')

    def test_timeout_maps_to_invalid_response_message(self):
        with patch('omni_ai.grok.urllib.request.urlopen', side_effect=TimeoutError()):
            with self.assertRaisesRegex(RuntimeError, 'Resposta inválida ou timeout da xAI'):
                grok.request('/models', 'k')

    def test_invalid_json_body_maps_to_invalid_response(self):
        with patch('omni_ai.grok.urllib.request.urlopen', return_value=FakeResp(b'not json')):
            with self.assertRaisesRegex(RuntimeError, 'Resposta inválida ou timeout'):
                grok.request('/models', 'k')

    def test_url_error_maps_to_network_failure(self):
        with patch('omni_ai.grok.urllib.request.urlopen', side_effect=urllib.error.URLError('down')):
            with self.assertRaisesRegex(RuntimeError, 'Falha de rede ao acessar a xAI'):
                grok.request('/models', 'k')

    def test_custom_name_in_error(self):
        with patch('omni_ai.grok.urllib.request.urlopen', side_effect=urllib.error.URLError('down')):
            with self.assertRaisesRegex(RuntimeError, 'Falha de rede ao acessar a DeepSeek'):
                grok.request('/models', 'k', name='DeepSeek')


class PickModelTests(unittest.TestCase):
    def _models(self, ids):
        return {'data': [{'id': i} for i in ids]}

    def test_prefers_fast_non_reasoning(self):
        with patch('omni_ai.grok.request', return_value=self._models(
                ['grok-4', 'grok-4-fast-non-reasoning', 'grok-4-fast'])):
            self.assertEqual(grok.pick_model('k'), 'grok-4-fast-non-reasoning')

    def test_falls_back_to_fast(self):
        with patch('omni_ai.grok.request', return_value=self._models(['grok-4', 'grok-3-fast'])):
            self.assertEqual(grok.pick_model('k'), 'grok-3-fast')

    def test_falls_back_to_any_grok(self):
        with patch('omni_ai.grok.request', return_value=self._models(['grok-2', 'grok-1'])):
            self.assertEqual(grok.pick_model('k'), 'grok-2')

    def test_raises_when_no_grok_available(self):
        with patch('omni_ai.grok.request', return_value=self._models(['gpt-x', 'claude-y'])):
            with self.assertRaisesRegex(RuntimeError, 'Nenhum modelo Grok disponível'):
                grok.pick_model('k')


class ChatTests(unittest.TestCase):
    def test_success_with_explicit_model_no_masking(self):
        def respond(endpoint, key, data=None, **kw):
            return {'choices': [{'message': {'content': 'hello back'}}], 'model': 'grok-x',
                    'usage': {'total_tokens': 5}}

        with patch('omni_ai.grok.request', side_effect=respond):
            out = grok.chat('k', 'hi there', model='grok-x', mask=False)
        self.assertEqual(out['content'], 'hello back')
        self.assertEqual(out['model'], 'grok-x')
        self.assertEqual(out['masked_pii_count'], 0)
        self.assertEqual(out['usage'], {'total_tokens': 5})

    def test_masking_hides_pii_in_request_and_restores_in_answer(self):
        seen = {}

        def respond(endpoint, key, data=None, **kw):
            seen['body'] = json.dumps(data)
            # Echo the (masked) user prompt back so unmask has something to restore.
            return {'choices': [{'message': {'content': data['messages'][-1]['content']}}]}

        trace = io.StringIO()
        with patch('omni_ai.grok.request', side_effect=respond), contextlib.redirect_stderr(trace):
            out = grok.chat('k', 'email alice@example.com please', model='m', mask=True)
        self.assertNotIn('alice@example.com', seen['body'])
        self.assertIn('alice@example.com', out['content'])  # unmasked back
        self.assertEqual(out['masked_pii_count'], 1)
        self.assertIn('PII mascarado', trace.getvalue())

    def test_system_prompt_is_prepended(self):
        seen = {}

        def respond(endpoint, key, data=None, **kw):
            seen['messages'] = data['messages']
            return {'choices': [{'message': {'content': 'ok'}}]}

        with patch('omni_ai.grok.request', side_effect=respond):
            grok.chat('k', 'hi', model='m', system_prompt='be nice', mask=False)
        self.assertEqual(seen['messages'][0], {'role': 'system', 'content': 'be nice'})
        self.assertEqual(seen['messages'][1], {'role': 'user', 'content': 'hi'})

    def test_empty_model_triggers_pick_model(self):
        with patch('omni_ai.grok.pick_model', return_value='grok-auto') as pick:
            with patch('omni_ai.grok.request',
                       return_value={'choices': [{'message': {'content': 'ok'}}]}) as req:
                out = grok.chat('k', 'hi', mask=False)
        pick.assert_called_once()
        self.assertEqual(req.call_args.args[2]['model'], 'grok-auto')
        self.assertEqual(out['model'], 'grok-auto')

    def test_events_trace_emitted_on_stderr(self):
        trace = io.StringIO()
        with patch.dict(os.environ, {'SMART_AI_EVENTS': '1'}):
            with patch('omni_ai.grok.request',
                       return_value={'choices': [{'message': {'content': 'ok'}}]}):
                with contextlib.redirect_stderr(trace):
                    grok.chat('k', 'hi', model='m', mask=False)
        lines = [json.loads(l) for l in trace.getvalue().splitlines() if l.startswith('{')]
        self.assertEqual(lines[0]['type'], 'smart.request')

    def test_malformed_response_raises_without_echoing(self):
        with patch('omni_ai.grok.request', return_value={'error': 'sensitive'}):
            with self.assertRaisesRegex(RuntimeError, 'não retornou uma resposta válida') as ctx:
                grok.chat('k', 'hi', model='m', mask=False)
        self.assertNotIn('sensitive', str(ctx.exception))

    def test_empty_content_raises(self):
        with patch('omni_ai.grok.request',
                   return_value={'choices': [{'message': {'content': '   '}}]}):
            with self.assertRaisesRegex(RuntimeError, 'conteúdo vazio'):
                grok.chat('k', 'hi', model='m', mask=False)

    def test_non_string_content_raises(self):
        with patch('omni_ai.grok.request',
                   return_value={'choices': [{'message': {'content': 123}}]}):
            with self.assertRaisesRegex(RuntimeError, 'conteúdo vazio'):
                grok.chat('k', 'hi', model='m', mask=False)


class MainTests(unittest.TestCase):
    def _run(self, argv, env=None, stdin='', isatty=True):
        out, err = io.StringIO(), io.StringIO()
        environ = dict(os.environ)
        environ.pop('SMART_AI_EVENTS', None)
        if env:
            environ.update(env)
        fake_stdin = io.StringIO(stdin)
        fake_stdin.isatty = lambda: isatty
        with patch.object(sys, 'argv', ['grok'] + argv), \
                patch.dict(os.environ, environ, clear=True), \
                patch.object(sys, 'stdin', fake_stdin), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                grok.main()
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue(), err.getvalue()

    def test_missing_key_exits_1(self):
        code, _out, err = self._run(['hi'], env={'XAI_API_KEY': ''})
        self.assertEqual(code, 1)
        self.assertIn('XAI_API_KEY', err)

    def test_no_prompt_prints_help_and_exits_1(self):
        code, out, _err = self._run([], env={'XAI_API_KEY': 'k'}, stdin='', isatty=True)
        self.assertEqual(code, 1)
        self.assertIn('usage', (out + _err).lower())

    def test_success_prints_content(self):
        with patch('omni_ai.grok.chat', return_value={'content': 'the answer', 'model': 'm',
                                                       'masked_pii_count': 0, 'usage': {}}):
            code, out, _err = self._run(['hello'], env={'XAI_API_KEY': 'k'})
        self.assertEqual(code, 0)
        self.assertIn('the answer', out)

    def test_prompt_read_from_stdin_when_not_tty(self):
        captured = {}

        def fake_chat(key, prompt, *a, **k):
            captured['prompt'] = prompt
            return {'content': 'x', 'model': 'm', 'masked_pii_count': 0, 'usage': {}}

        with patch('omni_ai.grok.chat', side_effect=fake_chat):
            code, _out, _err = self._run([], env={'XAI_API_KEY': 'k'}, stdin='piped prompt\n', isatty=False)
        self.assertEqual(code, 0)
        self.assertEqual(captured['prompt'], 'piped prompt')

    def test_runtime_error_exits_1(self):
        with patch('omni_ai.grok.chat', side_effect=RuntimeError('boom')):
            code, _out, err = self._run(['hi'], env={'XAI_API_KEY': 'k'})
        self.assertEqual(code, 1)
        self.assertIn('boom', err)

    def test_keyboard_interrupt_exits_130(self):
        with patch('omni_ai.grok.chat', side_effect=KeyboardInterrupt()):
            code, _out, _err = self._run(['hi'], env={'XAI_API_KEY': 'k'})
        self.assertEqual(code, 130)

    def test_events_mode_prints_json_answer(self):
        with patch('omni_ai.grok.chat', return_value={'content': 'x', 'model': 'm',
                                                      'masked_pii_count': 0, 'usage': {}}):
            code, out, _err = self._run(['hi'], env={'XAI_API_KEY': 'k', 'SMART_AI_EVENTS': '1'})
        self.assertEqual(code, 0)
        payload = json.loads(out.strip())
        self.assertEqual(payload['type'], 'smart.answer')


if __name__ == '__main__':
    unittest.main()
