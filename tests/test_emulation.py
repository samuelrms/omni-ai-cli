import isolation  # noqa: F401  (first: keeps the tests away from real data)
import http.client
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from test_server import ROOT, ServerCase

sys.path.insert(0, str(ROOT))
from omni_ai.server import emulation, protocol  # noqa: E402
from omni_ai.server.execution import OK, RATE_LIMITED  # noqa: E402

WEATHER = {'type': 'function', 'function': {'name': 'weather', 'parameters': {'type': 'object', 'required': ['city'], 'properties': {'city': {'type': 'string'}}}}}
USER = [{'role': 'user', 'content': 'weather?'}]
GOOD = '{"tool_calls": [{"name": "weather", "arguments": {"city": "Lisbon"}}]}'


def request(**fields):
    return protocol.parse_request({'model': 'omni/claude', 'messages': USER, 'tools': [WEATHER], **fields})


class Reply(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(emulation.parse_reply('{"content": "hi"}'), ('hi', []))
        self.assertEqual(emulation.parse_reply('plain text'), ('plain text', []))
        content, calls = emulation.parse_reply('```json\n' + GOOD + '\n```')
        self.assertEqual(json.loads(calls[0]['function']['arguments']), {'city': 'Lisbon'})
        self.assertEqual(calls[0]['type'], 'function')

    def test_broken(self):
        for text in ('{"tool_calls": [', '{"tool_calls": 1}', '{"tool_calls": ["x"]}', '{"tool_calls": [{"name": "a", "arguments": 1}]}', '[1]', '{"other": 1}'):
            with self.assertRaises(ValueError, msg=text):
                emulation.parse_reply(text)


class Prompt(unittest.TestCase):
    def test_carries_the_conversation(self):
        messages = USER + [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1', 'function': {'name': 'weather', 'arguments': '{"city":"x"}'}}]},
                           {'role': 'tool', 'tool_call_id': 'c1', 'content': '20 degrees'}]
        text = emulation.build_prompt(request(messages=messages))
        for piece in ('"weather"', 'called weather', '20 degrees', 'c1'):
            self.assertIn(piece, text)

    def test_choice_is_stated(self):
        self.assertIn('must call the function weather', emulation.build_prompt(request(tool_choice={'type': 'function', 'function': {'name': 'weather'}})))
        self.assertIn('Do not call', emulation.build_prompt(request(tool_choice='none')))

    def test_too_large(self):
        with self.assertRaises(protocol.RequestError):
            emulation.Emulated('claude', request(messages=[{'role': 'user', 'content': 'x' * 200_000}]))


class Target(unittest.TestCase):
    def test_only_clis_with_tools_off(self):
        everything = lambda name: True  # noqa: E731
        self.assertEqual(emulation.target('auto', everything), 'claude')
        self.assertEqual(emulation.target('grok', everything), 'grok')
        self.assertIsNone(emulation.target('codex', everything))
        self.assertIsNone(emulation.target('cursor', everything))
        self.assertIsNone(emulation.target('claude', lambda name: False))


class Retry(unittest.TestCase):
    def run_with(self, replies, outcome=OK, provider='claude'):
        prompts = []

        class Fake:
            child = None

            def __init__(self, tool, prompt, last, no_tools=False):
                assert no_tools
                prompts.append(prompt)
                self.provider, self.tokens, self.answer = provider, None, replies.pop(0)

            def run(self, _):
                return outcome

            def stop(self, reason):
                pass
        run = emulation.Emulated('claude', self.req)
        with patch.object(emulation, 'Execution', Fake):
            return run, run.run(), prompts

    def setUp(self):
        self.req = request()

    def test_invalid_json_then_good(self):
        run, outcome, prompts = self.run_with(['{"tool_calls": [', GOOD])
        self.assertEqual((outcome, run.tool_calls[0]['function']['name']), (OK, 'weather'))
        self.assertIn('rejected', prompts[1])

    def test_outside_the_schema_then_good(self):
        run, outcome, _ = self.run_with(['{"tool_calls": [{"name": "weather", "arguments": {}}]}', GOOD])
        self.assertEqual(outcome, OK)

    def test_one_retry_only(self):
        replies = ['{"tool_calls": ['] * 5
        _, outcome, prompts = self.run_with(replies)
        self.assertEqual((outcome, len(prompts)), ('failed', 2))

    def test_required_needs_a_call(self):
        self.req = request(tool_choice='required')
        _, outcome, prompts = self.run_with(['just text', 'just text'])
        self.assertEqual((outcome, len(prompts)), ('failed', 2))

    def test_plain_answer(self):
        run, outcome, _ = self.run_with(['{"content": "no tool needed"}'])
        self.assertEqual((outcome, run.answer, run.tool_calls), (OK, 'no tool needed', []))

    def test_limit_does_not_retry(self):
        _, outcome, prompts = self.run_with(['x', 'x'], outcome=RATE_LIMITED)
        self.assertEqual((outcome, len(prompts)), (RATE_LIMITED, 1))

    def test_fallback_to_a_tool_with_its_tools_on_is_refused(self):
        _, outcome, _ = self.run_with([GOOD], provider='codex')
        self.assertEqual(outcome, 'failed')


class OverHttp(ServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        claude = cls.root / 'claude'
        cls.original = claude.read_text()
        cls.emulated, cls.emulated_port, _ = cls.start('--tool-emulation', env={**cls.env, 'SMART_AI_SERVER_KEY': 'client'})

    @classmethod
    def tearDownClass(cls):
        cls.emulated.terminate()
        cls.emulated.wait(timeout=10)
        cls.emulated.stdout.close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        claude = self.root / 'claude'
        self.addCleanup(claude.write_text, self.original)
        answer = json.dumps({'type': 'result', 'result': GOOD})
        claude.write_text(f"#!/bin/sh\nfor a; do echo \"arg:$a\" >> \"$FAKE_LOG\"; done\nprintf '%s\\n' '{answer}'\n")

    def ask(self, port, key, **fields):
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
        self.addCleanup(connection.close)
        connection.request('POST', '/v1/chat/completions', json.dumps({'model': 'omni/claude', 'messages': USER, 'tools': [WEATHER], **fields}),
                           {'Authorization': f'Bearer {key}'})
        response = connection.getresponse()
        return response, response.read().decode()

    def test_off_by_default(self):
        response, text = self.ask(self.port, self.key)
        self.assertEqual((response.status, json.loads(text)['error']['code']), (400, 'tools_not_supported'))
        self.assertEqual(self.log.read_text(), '')

    def test_call_and_the_cli_has_no_tools(self):
        response, text = self.ask(self.emulated_port, 'client')
        body = json.loads(text)
        self.assertEqual((response.status, body['choices'][0]['finish_reason']), (200, 'tool_calls'))
        self.assertEqual(body['choices'][0]['message']['tool_calls'][0]['function']['name'], 'weather')
        calls = self.log.read_text()
        self.assertIn('arg:--tools\narg:\n', calls)
        self.assertIn('arg:--strict-mcp-config', calls)
        self.assertIn('arg:plan', calls)

    def test_streaming(self):
        response, text = self.ask(self.emulated_port, 'client', stream=True)
        self.assertEqual(response.status, 200)
        self.assertIn('tool_calls', text)
        self.assertIn('[DONE]', text)

    def test_codex_stays_refused(self):
        self.assertEqual(self.ask(self.emulated_port, 'client', model='omni/codex')[0].status, 400)


class GuardInRunners(unittest.TestCase):
    def test_tools_that_cannot_be_switched_off_do_not_run(self):
        from omni_ai.cli import executors
        with patch.dict('os.environ', {'SMART_AI_NO_TOOLS': '1'}), patch.object(executors, 'run_with_key_retry', side_effect=AssertionError):
            self.assertEqual(executors.run_codex('p'), 1)
            self.assertEqual(executors.run_cursor('p'), 1)
            self.assertEqual(executors.run_agy('p'), 1)


if __name__ == '__main__':
    unittest.main()
