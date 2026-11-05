"""Antigravity events, in the shape agy 1.5.1 really emits, through the chat runner."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import agy_events  # noqa: E402
from omni_ai.chat.runner import Runner  # noqa: E402

TOOL = {'step_type': 'tool', 'tool_name': 'run_command'}
START = {'event': 'step_update', 'step_update': {**TOOL, 'state': 'ACTIVE',
         'tool_info': {'name': 'run_command', 'parameters': {'CommandLine': 'ls -la'}}}}
DONE = {'event': 'step_update', 'step_update': {**TOOL, 'state': 'DONE',
        'tool_info': {'name': 'run_command', 'output': 'f.txt'}}}
READ = {'event': 'step_update', 'step_update': {'step_type': 'tool', 'tool_name': 'read_file', 'state': 'ACTIVE',
        'tool_info': {'name': 'read_file', 'parameters': {'Path': '/tmp/f.txt'}}}}
TEXT = {'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'state': 'ACTIVE', 'text_delta': 'hello'}}
SUCCESS = {'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'hello\n',
                                         'usage': {'input_tokens': 5, 'output_tokens': 2}}}


class FakeView:
    def __init__(self):
        self.actions, self.results, self.answers, self.errors = [], [], [], []

    def action(self, verb, target):
        self.actions.append((verb, target))

    def result(self, text, failed=False):
        self.results.append((text, failed))

    def answer(self, text):
        self.answers.append(text)

    def error(self, text):
        self.errors.append(text)

    def note(self, text):
        pass

    def provider(self, name, label):
        pass


class Parse(unittest.TestCase):
    def test_other_tools_keep_their_shape(self):
        self.assertFalse(agy_events.is_agy_event({'type': 'result', 'event': 'x'}))
        self.assertTrue(agy_events.is_agy_event(SUCCESS))

    def test_events_without_anything_to_act_on(self):
        for event in ({'event': 'init', 'init': {}}, TEXT, {'event': 'step_update', 'step_update': 'odd'}, {'event': 'result', 'result': 'odd'}):
            self.assertIsNone(agy_events.parse(event))

    def test_failed_result_and_tool(self):
        self.assertEqual(agy_events.parse({'event': 'result', 'result': {'status': 'ERROR', 'error': 'quota'}}), ('error', 'quota'))
        self.assertEqual(agy_events.parse({'event': 'result', 'result': {'status': 'ERROR'}}), ('error', 'ERROR'))
        self.assertEqual(agy_events.parse({'event': 'error', 'message': 'boom'}), ('error', 'boom'))
        failed = {'event': 'step_update', 'step_update': {**TOOL, 'state': 'ERROR', 'tool_info': {'output': 'no'}}}
        self.assertEqual(agy_events.parse(failed), ('tool_done', 'no', True))


class ChatRunner(unittest.TestCase):
    def job(self, *events):
        job = {'id': 't', 'engine': 'agy', 'sent': 'hi', 'answers': [], 'events': [], 'view': FakeView()}
        runner = Runner('.')
        runner.event(job, json.dumps({'type': 'smart.provider', 'provider': 'agy', 'profile': ''}))
        for event in events:
            runner.event(job, json.dumps(event))
        return job

    def test_result_is_the_answer_shown_once_and_counts_tokens(self):
        job = self.job(TEXT, SUCCESS)
        self.assertEqual(job['answers'], ['hello'])
        self.assertEqual(job['view'].answers, ['hello'])
        self.assertEqual(job['tokens'], 7)

    def test_tools_are_shown_like_the_other_providers(self):
        view = self.job(START, DONE, READ, SUCCESS)['view']
        self.assertEqual(view.actions[0], ('Ran', 'ls -la'))
        self.assertEqual(view.actions[1][0], 'Read')
        self.assertEqual(view.results, [('f.txt', False)])

    def test_failure_is_reported(self):
        job = self.job({'event': 'result', 'result': {'status': 'ERROR', 'error': 'quota'}})
        self.assertTrue(job['protocol_error'])
        self.assertEqual(job['view'].errors, ['quota'])

    @unittest.skipIf(os.name == 'nt', 'shell script as a fake tool')
    def test_full_run_ends_as_done(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake = root / 'agy'
            fake.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(json.dumps(SUCCESS)) + "\n")
            fake.chmod(0o700)
            env = {'AGY_BIN': str(fake), 'HOME': str(root), 'SMART_AI_HOME': str(root / 'c'),
                   'SMART_AI_STATE': str(root / 's'), 'SMART_AI_LOG': str(root / 'l')}
            saved = dict(os.environ)
            os.environ.update(env)
            self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
            job = {'id': 't', 'engine': 'agy', 'sent': 'hi', 'status': 'running'}
            Runner(folder).run(job, 'hi')
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['answers'], ['hello'])


if __name__ == '__main__':
    unittest.main()
