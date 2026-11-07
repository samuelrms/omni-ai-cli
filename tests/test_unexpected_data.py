"""Odd data from networks, tools and old files must not take the CLI or the chat down."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import ast
import http.client
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from omni_ai import config as config_tool, history
from omni_ai.chat.runner import Runner
from omni_ai.cli import base, classifier
from omni_ai.usage import providers

ROOT = Path(__file__).resolve().parent.parent


def reply(body):
    response = MagicMock()
    response.read.return_value = body
    response.__enter__ = lambda self: self
    response.__exit__ = lambda *_: False
    return response


class Classifier(unittest.TestCase):
    def test_a_connection_cut_midway_is_only_a_failure(self):
        with patch('urllib.request.urlopen', side_effect=http.client.IncompleteRead(b'x')):
            self.assertIsNone(classifier.ask_text('http://x', 'k', {}))
        response = reply(b'')
        response.read.side_effect = http.client.IncompleteRead(b'x')
        with patch('urllib.request.urlopen', return_value=response):
            self.assertIsNone(classifier.ask_text('http://x', 'k', {}))

    def test_odd_replies_are_only_a_failure(self):
        for body in (b'[]', b'{}', b'{"choices": []}', b'{"choices": [{"message": {"content": ["x"]}}]}', b'{"choices": "x"}', b'null'):
            with self.subTest(body=body), patch('urllib.request.urlopen', return_value=reply(body)):
                self.assertIsNone(classifier.ask_classifier('http://x', 'k', {}))


class UsageReadings(unittest.TestCase):
    def test_odd_usage_answers_read_as_nothing(self):
        for body in (b'[]', b'null', b'1'):
            with self.subTest(body=body), patch('urllib.request.urlopen', return_value=reply(body)), \
                    patch.object(providers, 'claude_token', return_value='t', create=True):
                self.assertEqual(providers.claude_windows(json.loads(body)), [])

    def test_a_connection_cut_midway_reads_as_nothing(self):
        with patch('urllib.request.urlopen', side_effect=http.client.IncompleteRead(b'')), \
                patch.object(providers, 'claude_token', return_value='t', create=True):
            self.assertIsNone(providers.claude_limits({}))

    def test_odd_account_status_reads_as_nothing(self):
        with patch.object(providers.subprocess, 'run', return_value=MagicMock(stdout='[]')):
            self.assertIsNone(providers.claude_account({}))

    def test_odd_credit_answer_reads_as_nothing(self):
        with patch('urllib.request.urlopen', return_value=reply(b'[]')):
            self.assertIsNone(providers.openrouter_credits('k'))


class Files(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_only_a_missing_file_reads_as_empty(self):
        self.assertEqual(config_tool.read_json(self.root / 'gone.json', {}), {})
        self.assertEqual(config_tool.read_json(self.root / 'gone' / 'deeper.json', {}), {})

    @unittest.skipIf(os.name == 'nt' or (hasattr(os, 'geteuid') and os.geteuid() == 0), 'needs POSIX permissions')
    def test_an_unreadable_file_is_not_overwritten(self):
        path = self.root / 'state.json'
        path.write_text('{"keep": 1}')
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        with self.assertRaises(SystemExit):
            config_tool.update_json(path, lambda data: data.update({'new': 2}))
        path.chmod(0o600)
        self.assertEqual(json.loads(path.read_text()), {'keep': 1})

    def test_a_list_where_an_object_belongs_is_reported(self):
        path = self.root / 'state.json'
        path.write_text('[]')
        with self.assertRaises(SystemExit) as caught:
            config_tool.read_json(path, {})
        self.assertIn(str(path), str(caught.exception))
        self.assertEqual(path.read_text(), '[]')

    def test_a_folder_in_place_of_a_file_is_reported(self):
        with self.assertRaises(SystemExit):
            config_tool.read_json(self.root, {})

    def test_cooldowns_read_with_a_broken_state_file_are_a_clear_message(self):
        path = self.root / 'state.json'
        path.write_text('[]')
        with patch.object(base, 'STATE_FILE', path), self.assertRaises(SystemExit):
            base.state()


class Encoding(unittest.TestCase):
    def test_every_text_file_names_its_encoding(self):
        """The default depends on the machine, and a latin-1 one cannot save a check mark or emoji."""
        missing = []
        for path in (ROOT / 'omni_ai').rglob('*.py'):
            if 'vendor' in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
                keywords = {item.arg for item in node.keywords}
                if name in ('read_text', 'write_text'):
                    risky = True
                elif name in ('open', 'fdopen') and not (isinstance(node.func, ast.Attribute) and name == 'open' and node.func.attr != 'open'):
                    mode = node.args[1] if len(node.args) > 1 else next((item.value for item in node.keywords if item.arg == 'mode'), None)
                    risky = not (isinstance(mode, ast.Constant) and isinstance(mode.value, str) and 'b' in mode.value) and bool(node.args)
                    risky = risky and not (isinstance(node.func, ast.Attribute) and getattr(node.func.value, 'id', '') == 'os' and name == 'open')
                    risky = risky and not (isinstance(node.func, ast.Attribute) and getattr(node.func.value, 'id', '') in ('webbrowser', 'selectors'))
                else:
                    continue
                if risky and 'encoding' not in keywords:
                    missing.append(f'{path.relative_to(ROOT)}:{node.lineno}')
        self.assertEqual(missing, [])

    @unittest.skipIf(os.name == 'nt', 'POSIX locale')
    def test_a_session_saves_on_a_machine_without_utf8(self):
        code = ("from omni_ai.chat.session import Session; s = Session(); s.data['messages'].append({'role': 'user', 'content': '✓ 日本語 😀'}); "
                "s.save(); s.close(); r = Session(s.id); print(r.data['messages'][0]['content'] == '✓ 日本語 😀')")
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, 'HOME': home, 'PYTHONPATH': str(ROOT), 'LC_ALL': 'C', 'LANG': 'C', 'PYTHONCOERCECLOCALE': '0', 'PYTHONUTF8': '0',
                   'SMART_AI_HOME': home}
            result = subprocess.run([sys.executable, '-P', '-c', code], env=env, capture_output=True, text=True, cwd=home)
        self.assertEqual(result.stdout.strip(), 'True', result.stderr[-600:])


class SavedQueue(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.folder = self.root / 'sessions'
        self.folder.mkdir(parents=True)
        self.env = {**os.environ, 'HOME': str(self.root), 'PYTHONPATH': str(ROOT), 'SMART_AI_SESSIONS': str(self.folder),
                    'SMART_AI_HOME': str(self.root / 'cfg')}

    def session(self, **fields):
        identifier = 'a' * 12
        (self.folder / f'{identifier}.json').write_text(json.dumps({'id': identifier, **fields}), encoding='utf-8')
        return identifier

    def resume(self, identifier):
        return subprocess.run([sys.executable, '-P', '-m', 'omni_ai.chat', '--resume', identifier], env=self.env, cwd=self.root,
                              input='', capture_output=True, text=True, timeout=60)

    def test_without_an_input_box_the_saved_queue_stays(self):
        queue = [{'label': 'first', 'kind': 'ask', 'engine': 'auto', 'text': 'first'}]
        identifier = self.session(cwd=str(self.root), engine='auto', messages=[], requests=[], queue=queue)
        result = self.resume(identifier)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        saved = json.loads((self.folder / f'{identifier}.json').read_text(encoding='utf-8'))
        self.assertEqual(saved.get('queue'), queue)

    def test_a_session_with_missing_fields_resumes(self):
        identifier = self.session(messages='not a list')
        result = self.resume(identifier)
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        saved = json.loads((self.folder / f'{identifier}.json').read_text(encoding='utf-8'))
        self.assertEqual((saved['messages'], saved['requests']), ([], []))

    def test_a_session_that_is_not_an_object_is_refused_clearly(self):
        identifier = 'b' * 12
        (self.folder / f'{identifier}.json').write_text('[]', encoding='utf-8')
        result = self.resume(identifier)
        self.assertNotIn('Traceback', result.stderr)


class ToolOutput(unittest.TestCase):
    def runner(self):
        runner = Runner.__new__(Runner)
        runner.cancelled, runner.lock, runner.children = threading.Event(), threading.Lock(), set()
        return runner

    def test_odd_events_do_not_end_the_job(self):
        for line in ('{"item": "x"}', '{"type": "assistant", "message": "x"}', '{"type": "assistant", "message": {"content": "x"}}',
                     '{"type": "assistant", "message": {"content": ["x"]}}', '{"type": "item.completed", "item": 1}',
                     '{"type": "smart.provider", "provider": []}', '{"type": "result", "usage": 1}', '{"type": "smart.delta", "text": 1}',
                     '{"type": "smart.answer", "content": 1}', '{"type": "turn.completed", "usage": "x"}', '[]', '1', 'null'):
            with self.subTest(line=line):
                job = {'id': 1, 'engine': 'codex', 'view': MagicMock(), 'events': [], 'answers': [], 'dispatches': []}
                self.runner().event(job, line)


class History(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME'):
            patcher = patch.dict(os.environ, {name: str(self.root)})
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, folder, name, lines):
        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(line) for line in lines), encoding='utf-8')

    def test_one_bad_claude_file_keeps_the_others(self):
        good = {'type': 'user', 'message': {'role': 'user', 'content': 'good'}, 'cwd': '/x'}
        self.write('projects/p', 'good.jsonl', [good])
        self.write('projects/p', 'bad.jsonl', [{'type': 'user', 'message': {'content': [1, {'type': 'text', 'text': 5}]}, 'cwd': 1}])
        self.assertIn('claude:good', [item['id'] for item in history._read_claude()])

    def test_one_bad_codex_file_keeps_the_others(self):
        self.write('sessions', 'good.jsonl', [{'payload': {'role': 'user', 'content': 'good'}}])
        self.write('sessions', 'bad.jsonl', [{'payload': {'role': 'user', 'content': [{'text': 5}]}}])
        self.assertIn('codex:good', [item['id'] for item in history._read_codex()])

    def test_a_line_that_cannot_be_used_keeps_the_rest_of_the_file(self):
        self.write('projects/p', 'mixed.jsonl', [{'type': 'user', 'message': {'content': [{'type': 'text', 'text': 5}]}},
                                                 {'type': 'user', 'message': {'content': 'kept'}}])
        items = list(history._read_claude())
        self.assertEqual(items[0]['messages'][-1]['content'], 'kept')


if __name__ == '__main__':
    unittest.main()
