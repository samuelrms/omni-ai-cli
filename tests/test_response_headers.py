"""What a tool prints must not become part of the server's HTTP headers."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import unittest
from unittest.mock import patch

from test_server import ServerCase

from omni_ai.server.execution import Execution
from omni_ai.server.handler import Handler

CODEX = r"""#!/bin/sh
case "$*" in
  *splitting*) echo '{"type":"smart.provider","provider":"codex\r\nSet-Cookie: stolen=1"}' ;;
  *unknown*) echo '{"type":"smart.provider","provider":"not-a-tool"}' ;;
  *notext*) echo '{"type":"smart.provider","provider":["codex"]}' ;;
esac
echo '{"type":"item.completed","item":{"type":"agent_message","text":"answer"}}'
"""


class ProviderFromATool(ServerCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.root / 'codex').write_text(CODEX)

    def assert_clean(self, response):
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('X-Omni-Provider'), 'codex')
        self.assertIsNone(response.getheader('Set-Cookie'))

    def test_a_line_break_does_not_add_a_header(self):
        self.assert_clean(self.chat('splitting')[0])

    def test_a_line_break_does_not_add_a_header_when_streaming(self):
        self.assert_clean(self.chat('splitting', stream=True)[0])

    def test_an_unknown_provider_is_not_reported(self):
        self.assert_clean(self.chat('unknown')[0])

    def test_a_provider_that_is_not_text_is_not_reported(self):
        self.assert_clean(self.chat('notext')[0])


class ProviderEvent(unittest.TestCase):
    def provider_after(self, line):
        execution = Execution('codex', 'p', 'p')
        execution._line(line)
        return execution.provider

    def test_known_tools_are_kept(self):
        for name in ('claude', 'codex', 'agy', 'cursor', 'grok', 'openrouter', 'deepseek'):
            with self.subTest(name=name):
                self.assertEqual(self.provider_after(f'{{"type":"smart.provider","provider":"{name}"}}'), name)

    def test_anything_else_is_dropped(self):
        for value in ('"x"', '"codex\\r\\nA: b"', '["codex"]', '{"a":1}', '1', 'null'):
            with self.subTest(value=value):
                self.assertIsNone(self.provider_after(f'{{"type":"smart.provider","provider":{value}}}'))

    def test_a_new_attempt_still_forgets_the_previous_one(self):
        execution = Execution('codex', 'p', 'p')
        execution.answer = 'old'
        execution._line('{"type":"smart.provider","provider":"not-a-tool"}')
        self.assertEqual(execution.answer, '')


class EveryHeader(unittest.TestCase):
    def written(self, value):
        handler = Handler.__new__(Handler)
        handler.request_version = 'HTTP/1.1'
        handler._headers_buffer = []
        with patch('omni_ai.server.handler.REQUEST_LOG') as log:
            handler.send_header('X-Test', value)
        return handler._headers_buffer, log

    def test_control_characters_keep_the_header_out(self):
        for value in ('a\r\nB: c', 'a\nb', 'a\rb', 'a\x00b', 'a\x7fb', 'a\x85b', 'ação b', '日本'):
            with self.subTest(value=value):
                buffer, log = self.written(value)
                self.assertEqual(buffer, [])
                log.warning.assert_called_once()

    def test_normal_values_are_written(self):
        for value in ('codex', 'temperature, top_p', 'omni/0123456789abcdef', '200', 'a\tb', 'café'):
            with self.subTest(value=value):
                buffer, log = self.written(value)
                self.assertEqual(len(buffer), 1)
                log.warning.assert_not_called()

    def test_numbers_still_work(self):
        self.assertEqual(self.written(12)[0], [b'X-Test: 12\r\n'])


if __name__ == '__main__':
    unittest.main()
