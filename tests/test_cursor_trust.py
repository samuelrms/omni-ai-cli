"""The Cursor CLI is trusted with a folder only when the person says so.

The fake below behaves like the real Cursor CLI 2026.10: in print mode it refuses a folder it does not
trust yet unless it gets --trust or --force; --trust keeps the trust for that folder, --force does not."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from omni_ai.cli import executors

FAKE_CURSOR = '''#!{python}
import json, os, sys
args = sys.argv[1:]
with open({calls!r}, 'a', encoding='utf-8') as out:
    out.write(json.dumps({{'args': args, 'key': os.environ.get('CURSOR_API_KEY')}}) + '\\n')
if '--trust' in args:
    open('.trusted-by-cursor', 'w').close()
if not ({{'--trust', '--force'}} & set(args) or os.path.exists('.trusted-by-cursor')):
    print('\\u26a0 Workspace Trust Required')
    print('  Pass --trust, --yolo, or -f if you trust this directory')
    sys.exit(1)
print('OK')
'''
MODELS = {'claude': '', 'codex': '', 'codex_effort': '', 'cursor': '', 'agy': '',
          'openrouter': '', 'openrouter_paid': 'off', 'grok': '', 'deepseek': ''}


class Terminal(io.StringIO):
    def isatty(self):
        return True


@unittest.skipIf(os.name == 'nt', 'the fake CLI is a script with a shebang')
class CursorTrust(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.folder = root / 'project'
        self.folder.mkdir()
        self.calls = root / 'calls.jsonl'
        fake = root / 'cursor-agent'
        fake.write_text(FAKE_CURSOR.format(python=sys.executable, calls=str(self.calls)), encoding='utf-8')
        fake.chmod(0o755)
        here = os.getcwd()
        os.chdir(self.folder)
        self.addCleanup(os.chdir, here)
        self.said = []
        for patcher in (
            patch.dict(executors.BINS, cursor=str(fake)),
            patch.object(executors, 'installed', return_value=True),
            patch.object(executors, 'models', return_value=MODELS),
            patch.object(executors, 'say', side_effect=lambda text='': self.said.append(text)),
            patch.object(executors, 'header'),
            patch.object(executors, 'cooldown_left', return_value=0),
            patch.object(executors.config_tool, 'tool_accounts', return_value=[]),
            patch.object(executors.config_tool, 'stored_keys', return_value=[]),
            patch('sys.stdout', io.StringIO()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cursor(self, mode, terminal=False, answer=''):
        stdin = Terminal() if terminal else io.StringIO()
        stdout = Terminal() if terminal else io.StringIO()
        with patch.object(executors, 'mode', return_value=mode), patch('sys.stdin', stdin), \
                patch('sys.stdout', stdout), patch('builtins.input', return_value=answer) as asked:
            code = executors.run_cursor('explain this project')
        return code, asked

    def cursor_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text(encoding='utf-8').splitlines()]

    def test_read_only_mode_does_not_trust_the_folder_on_its_own(self):
        code, asked = self.run_cursor('plan')
        self.assertEqual(code, 1)
        asked.assert_not_called()
        self.assertEqual(len(self.cursor_calls()), 1)
        self.assertNotIn('--trust', self.cursor_calls()[0]['args'])
        self.assertFalse((self.folder / '.trusted-by-cursor').exists())
        self.assertIn('ainda não confia', '\n'.join(self.said))

    def test_other_accounts_and_keys_are_not_tried_for_an_untrusted_folder(self):
        with patch.object(executors.config_tool, 'tool_accounts', return_value=[('work', {})]), \
                patch.object(executors.config_tool, 'tool_env', return_value={}), \
                patch.object(executors.config_tool, 'stored_keys', return_value=[('main', 'key-1', 'saved')]):
            self.assertEqual(self.run_cursor('plan')[0], 1)
        self.assertEqual(len(self.cursor_calls()), 1)

    def test_in_a_terminal_the_person_can_trust_the_folder(self):
        code, asked = self.run_cursor('plan', terminal=True, answer='s')
        self.assertEqual(code, 0)
        asked.assert_called_once()
        self.assertIn(str(self.folder), asked.call_args[0][0])
        first, second = self.cursor_calls()
        self.assertNotIn('--trust', first['args'])
        self.assertIn('--trust', second['args'])
        self.assertEqual(second['args'][-1], 'explain this project')

    def test_the_answer_is_kept_by_cursor_so_it_is_asked_once(self):
        self.run_cursor('plan', terminal=True, answer='s')
        code, asked = self.run_cursor('plan', terminal=True)
        self.assertEqual(code, 0)
        asked.assert_not_called()
        self.assertNotIn('--trust', self.cursor_calls()[-1]['args'])

    def test_saying_no_keeps_the_folder_untrusted(self):
        for answer in ('', 'n', 'nao'):
            with self.subTest(answer=answer):
                code, asked = self.run_cursor('plan', terminal=True, answer=answer)
                self.assertEqual(code, 1)
                asked.assert_called_once()
                self.assertTrue(all('--trust' not in call['args'] for call in self.cursor_calls()))
                self.assertFalse((self.folder / '.trusted-by-cursor').exists())

    def test_modes_that_edit_work_without_trusting_the_folder(self):
        for mode in ('edits', 'auto', 'yolo'):
            with self.subTest(mode=mode):
                code, asked = self.run_cursor(mode)
                self.assertEqual(code, 0)
                asked.assert_not_called()
                args = self.cursor_calls()[-1]['args']
                self.assertIn('--force', args)
                self.assertNotIn('--trust', args)
        self.assertFalse((self.folder / '.trusted-by-cursor').exists())

    def test_a_folder_already_trusted_in_cursor_just_works(self):
        (self.folder / '.trusted-by-cursor').touch()
        code, asked = self.run_cursor('plan')
        self.assertEqual(code, 0)
        asked.assert_not_called()
        self.assertEqual(len(self.cursor_calls()), 1)


if __name__ == '__main__':
    unittest.main()
