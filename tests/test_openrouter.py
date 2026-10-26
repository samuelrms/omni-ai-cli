import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)
sys.path.insert(0, str(ROOT))
from omni_ai.openrouter import OpenRouterClient
from omni_ai.pii import PIIMasker


class ClientTests(unittest.TestCase):
    def test_mask_both_messages_and_restore_separately(self):
        client = OpenRouterClient(api_key='synthetic-key', fallback_models=['test/a:free', 'test/b:free'])
        def respond(endpoint, data=None, method='GET'):
            self.assertEqual(data['models'], ['test/a:free', 'test/b:free', 'openrouter/free'])
            self.assertEqual(data['provider']['max_price'], {'prompt': 0, 'completion': 0})
            body = json.dumps(data)
            self.assertNotIn('alice@example.com', body)
            self.assertNotIn('bob@example.com', body)
            return {'choices': [{'message': {'content': ' '.join(m['content'] for m in data['messages'])}}], 'model': 'test/b:free'}
        with patch.object(client, '_request', side_effect=respond):
            answer = client.chat('alice@example.com', system_prompt='bob@example.com')
        self.assertEqual(answer['content'], 'bob@example.com alice@example.com')

    def test_reject_paid_models(self):
        with self.assertRaises(ValueError):
            OpenRouterClient(model='paid/model')
        with self.assertRaises(ValueError):
            OpenRouterClient(fallback_models=['paid/model'])

    def test_paid_model_requires_authorization_and_lifts_price_cap(self):
        with self.assertRaises(ValueError):
            OpenRouterClient(api_key='synthetic-key', model='vendor/paid')
        client = OpenRouterClient(api_key='synthetic-key', model='vendor/paid', fallback_models=['test/a:free'], allow_paid=True)
        with patch.object(client, '_request', return_value={'choices': [{'message': {'content': 'ok'}}]}) as request:
            client.chat('oi')
        data = request.call_args.kwargs['data']
        self.assertEqual(data['models'][0], 'vendor/paid')
        self.assertNotIn('provider', data)

    def test_catalog_unavailable_still_uses_free_router(self):
        client = OpenRouterClient(api_key='fake')
        with patch.object(client, 'list_free_models', side_effect=RuntimeError('unavailable')):
            with patch.object(client, '_request', return_value={'choices': [{'message': {'content': 'ok'}}]}) as request:
                client.chat('hello')
                self.assertEqual(request.call_args.kwargs['data']['models'], ['openrouter/free'])

    def test_invalid_response_does_not_echo_body(self):
        client = OpenRouterClient(api_key='fake', fallback_models=[])
        with patch.object(client, '_request', return_value={'error': 'sensitive data'}):
            with self.assertRaisesRegex(RuntimeError, 'não retornou') as raised:
                client.chat('hello')
            self.assertNotIn('sensitive', str(raised.exception))

    def test_mask_roundtrip_compound_credentials_and_repeated_email(self):
        original = 'postgres://person:password@host/db email: alice@example.com alice@example.com nome: Maria Silva'
        masker = PIIMasker()
        masked, mapping = masker.mask(original)
        self.assertNotIn('password', masked)
        self.assertNotIn('alice@example.com', masked)
        self.assertNotIn('Maria Silva', masked)
        self.assertEqual(masker.unmask(masked, mapping), original)

    def test_known_placeholder_in_input_preserved(self):
        masker = PIIMasker()
        original = '[EMAIL_SYSTEM_1] alice@example.com'
        masked, mapping = masker.mask(original, namespace='SYSTEM')
        self.assertEqual(masker.unmask(masked, mapping), original)


class ShellTests(unittest.TestCase):
    def test_forced_openrouter_and_auto_fallback_without_prompt_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            import shutil
            shutil.copytree(ROOT / 'omni_ai', root / 'omni_ai', ignore=shutil.ignore_patterns('__pycache__', 'vendor'))
            (root / 'profiles.json').write_text(json.dumps({'routing': {'prefer_tool_for_simple': 'openrouter', 'fallback_order': ['openrouter', 'codex']}}))
            (root / 'omni_ai' / 'openrouter.py').write_text('import os,sys\nprompt=sys.stdin.read()\nprint("ROUTER_OK")\nsys.exit(int(os.environ.get("TEST_FAILURE", "0")))\n')
            codex = root / 'fake-codex'
            codex.write_text('#!/bin/sh\necho CODEX_OK\n')
            codex.chmod(0o700)
            env = dict(os.environ, PYTHONPATH=str(root), SMART_AI_CONFIG=str(root/'profiles.json'), SMART_AI_HOME=str(root/'cfg'), OPENROUTER_API_KEY='synthetic', SMART_AI_STATE=str(root/'state'), SMART_AI_LOG=str(root/'log'), CODEX_BIN=str(codex))
            prompt = 'alice@example.com'
            forced = subprocess.run([sys.executable, '-m', 'omni_ai.cli', '--openrouter', prompt], env=env, capture_output=True, text=True, cwd=root)
            self.assertEqual(forced.returncode, 0, forced.stderr)
            self.assertIn('ROUTER_OK', forced.stdout)
            self.assertNotIn('CODEX_OK', forced.stdout)
            env['TEST_FAILURE'] = '1'
            automatic = subprocess.run([sys.executable, '-m', 'omni_ai.cli', prompt], env=env, capture_output=True, text=True, cwd=root)
            self.assertEqual(automatic.returncode, 0, automatic.stderr)
            self.assertIn('CODEX_OK', automatic.stdout)
            self.assertNotIn(prompt, (root/'log').read_text())


@unittest.skipUnless(os.getenv('SMART_AI_SIM_URL'), 'local Postman simulation not enabled')
class HttpTests(unittest.TestCase):
    def test_real_http_masking_fallback_contract_and_quota(self):
        with patch('omni_ai.openrouter.OPENROUTER_BASE', os.environ['SMART_AI_SIM_URL']):
            client = OpenRouterClient(api_key='synthetic-key')
            trace = io.StringIO()
            with patch.dict(os.environ, SMART_AI_EVENTS='1'), contextlib.redirect_stderr(trace):
                result = client.chat('alice@example.com', system_prompt='bob@example.com')
            events = [json.loads(line) for line in trace.getvalue().splitlines() if line.startswith('{')]
            self.assertEqual(events[0]['type'], 'smart.request')
            self.assertNotIn('alice@example.com', trace.getvalue())
            self.assertNotIn('bob@example.com', trace.getvalue())
            self.assertEqual(result['model'], 'test/b:free')
            self.assertEqual(result['content'], 'bob@example.com alice@example.com')
            with self.assertRaisesRegex(RuntimeError, 'HTTP 429'):
                client.chat('QUOTA')

if __name__ == '__main__':
    unittest.main()
