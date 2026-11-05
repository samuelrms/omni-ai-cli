import isolation  # noqa: F401  (first: keeps the tests away from real data)
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)
sys.path.insert(0, str(ROOT))
from omni_ai import deepseek


class ChatTests(unittest.TestCase):
    def test_delegates_to_grok_with_deepseek_base_and_name(self):
        with patch('omni_ai.grok.chat', return_value={'content': 'ok'}) as grok_chat:
            out = deepseek.chat('k', 'hi', mask=False)
        self.assertEqual(out, {'content': 'ok'})
        _args, kwargs = grok_chat.call_args
        self.assertEqual(kwargs['base'], deepseek.DEEPSEEK_BASE)
        self.assertEqual(kwargs['name'], 'DeepSeek')

    def test_empty_model_defaults_to_deepseek_chat(self):
        with patch('omni_ai.grok.chat', return_value={'content': 'ok'}) as grok_chat:
            deepseek.chat('k', 'hi', model='', mask=False)
        # grok.chat(key, prompt, model_or_default, system_prompt, mask, base=, name=)
        self.assertEqual(grok_chat.call_args.args[2], deepseek.DEFAULT_MODEL)

    def test_explicit_model_is_passed_through(self):
        with patch('omni_ai.grok.chat', return_value={'content': 'ok'}) as grok_chat:
            deepseek.chat('k', 'hi', model='deepseek-reasoner', mask=False)
        self.assertEqual(grok_chat.call_args.args[2], 'deepseek-reasoner')


class MainTests(unittest.TestCase):
    def _run(self, argv, env=None, stdin='', isatty=True):
        out, err = io.StringIO(), io.StringIO()
        environ = dict(os.environ)
        environ.pop('SMART_AI_EVENTS', None)
        if env:
            environ.update(env)
        fake_stdin = io.StringIO(stdin)
        fake_stdin.isatty = lambda: isatty
        with patch.object(sys, 'argv', ['deepseek'] + argv), \
                patch.dict(os.environ, environ, clear=True), \
                patch.object(sys, 'stdin', fake_stdin), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                deepseek.main()
                code = 0
            except SystemExit as e:
                code = e.code or 0
        return code, out.getvalue(), err.getvalue()

    def test_missing_key_exits_1(self):
        code, _out, err = self._run(['hi'], env={'DEEPSEEK_API_KEY': ''})
        self.assertEqual(code, 1)
        self.assertIn('DEEPSEEK_API_KEY', err)

    def test_no_prompt_prints_help_and_exits_1(self):
        code, out, err = self._run([], env={'DEEPSEEK_API_KEY': 'k'}, stdin='', isatty=True)
        self.assertEqual(code, 1)
        self.assertIn('usage', (out + err).lower())

    def test_success_prints_content(self):
        with patch('omni_ai.deepseek.chat', return_value={'content': 'resposta', 'model': 'deepseek-chat',
                                                          'masked_pii_count': 0, 'usage': {}}):
            code, out, _err = self._run(['hello'], env={'DEEPSEEK_API_KEY': 'k'})
        self.assertEqual(code, 0)
        self.assertIn('resposta', out)

    def test_prompt_from_stdin_when_not_tty(self):
        captured = {}

        def fake_chat(key, prompt, *a, **k):
            captured['prompt'] = prompt
            return {'content': 'x', 'model': 'm', 'masked_pii_count': 0, 'usage': {}}

        with patch('omni_ai.deepseek.chat', side_effect=fake_chat):
            code, _out, _err = self._run([], env={'DEEPSEEK_API_KEY': 'k'},
                                         stdin='piped\n', isatty=False)
        self.assertEqual(code, 0)
        self.assertEqual(captured['prompt'], 'piped')

    def test_runtime_error_exits_1(self):
        with patch('omni_ai.deepseek.chat', side_effect=RuntimeError('boom')):
            code, _out, err = self._run(['hi'], env={'DEEPSEEK_API_KEY': 'k'})
        self.assertEqual(code, 1)
        self.assertIn('boom', err)

    def test_keyboard_interrupt_exits_130(self):
        with patch('omni_ai.deepseek.chat', side_effect=KeyboardInterrupt()):
            code, _out, _err = self._run(['hi'], env={'DEEPSEEK_API_KEY': 'k'})
        self.assertEqual(code, 130)

    def test_events_mode_prints_json_answer(self):
        with patch('omni_ai.deepseek.chat', return_value={'content': 'x', 'model': 'm',
                                                         'masked_pii_count': 0, 'usage': {}}):
            code, out, _err = self._run(['hi'], env={'DEEPSEEK_API_KEY': 'k', 'SMART_AI_EVENTS': '1'})
        self.assertEqual(code, 0)
        payload = json.loads(out.strip())
        self.assertEqual(payload['type'], 'smart.answer')


if __name__ == '__main__':
    unittest.main()
