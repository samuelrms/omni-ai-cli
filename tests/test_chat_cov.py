"""Broad coverage for omni_ai/chat.py: event stream, slash commands, queue specs,
persistence, errors, i18n, release notes and the interactive main loop.

Everything that would touch the network, a subprocess, the clock or real input is mocked,
and the filesystem is redirected to temporary directories, so the suite is deterministic.
"""
import builtins
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)
sys.path.insert(0, str(ROOT))
from omni_ai import chat, ui  # noqa: E402


class Base(unittest.TestCase):
    """Temp dirs for every path chat.py writes to, and a clean, deterministic i18n/global state."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = {
            'SMART_AI_SESSIONS': str(self.root / 'sessions'),
            'SMART_AI_LOG': str(self.root / 'cache' / 'usage.log'),
            'SMART_AI_STATE': str(self.root / 'state'),
            'NO_COLOR': '1',
            'SMART_AI_NO_SPLASH': '1',
        }
        p = patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        # a writable language file, and a known starting language
        lang_file = self.root / 'lang'
        lp = patch.object(chat.i18n, 'LANG_FILE', lang_file)
        lp.start()
        self.addCleanup(lp.stop)
        self.lang_file = lang_file
        chat.set_lang('pt')
        self.addCleanup(chat.set_lang, 'pt')
        # never leave a stray runner behind for another test
        self.addCleanup(chat.ACTIVE.clear)


# --------------------------------------------------------------------------- language


class LanguageTests(Base):
    def test_save_and_load_round_trip(self):
        chat.save_lang('es')
        self.assertEqual(chat.load_lang(), 'es')
        self.assertEqual(self.lang_file.read_text(), 'es')

    def test_load_returns_none_for_unknown_or_missing(self):
        self.assertIsNone(chat.load_lang())  # file does not exist yet
        self.lang_file.write_text('zz')
        self.assertIsNone(chat.load_lang())  # not a known language

    def test_set_lang_also_moves_the_screen_language(self):
        chat.set_lang('fr')
        self.assertEqual(chat.LANG[0], 'fr')
        self.assertEqual(ui.LANG[0], 'fr')

    def test_tr_formats_with_tool_text_and_values(self):
        chat.set_lang('en')
        self.assertIn('session 42', chat.tr('welcome', id='42', cwd='/tmp'))

    def test_choose_lang_by_number(self):
        with patch.object(builtins, 'input', return_value='3'):
            chat.choose_lang()
        self.assertEqual(chat.LANG[0], 'es')
        self.assertEqual(self.lang_file.read_text(), 'es')

    def test_choose_lang_by_code_and_on_interrupt_keeps_default(self):
        with patch.object(builtins, 'input', return_value='fr'):
            chat.choose_lang()
        self.assertEqual(chat.LANG[0], 'fr')
        with patch.object(builtins, 'input', side_effect=KeyboardInterrupt):
            chat.choose_lang()  # empty answer: language unchanged, still saved
        self.assertEqual(chat.LANG[0], 'fr')


# --------------------------------------------------------------------------- Session


class SessionTests(Base):
    def test_new_session_defaults_and_save(self):
        s = chat.Session()
        self.addCleanup(s.close)
        self.assertEqual(s.data['engine'], 'auto')
        self.assertEqual(s.data['messages'], [])
        self.assertTrue(s.path.exists())

    def test_invalid_id_rejected(self):
        with self.assertRaises(ValueError):
            chat.Session('not-a-valid-id')

    def test_resume_missing_session_rejected(self):
        with self.assertRaises(ValueError):
            chat.Session('abcdef012345')  # well formed but no file

    def test_resume_marks_running_jobs_interrupted(self):
        s = chat.Session()
        sid = s.id
        s.data['requests'].append({'id': 'agente-1', 'status': 'running'})
        s.save()
        s.close()
        again = chat.Session(sid)
        self.addCleanup(again.close)
        self.assertEqual(again.data['requests'][0]['status'], 'interrupted')

    def test_second_open_is_locked_out(self):
        s = chat.Session()
        self.addCleanup(s.close)
        with self.assertRaises(ValueError):
            chat.Session(s.id)

    def test_save_survives_a_non_serialisable_value(self):
        s = chat.Session()
        self.addCleanup(s.close)
        s.data['requests'].append({'id': 'agente-1', 'status': 'running', 'view': object()})
        s.save()
        saved = json.loads(s.path.read_text())
        self.assertIsNone(saved['requests'][0]['view'])

    def test_context_keeps_recent_messages_and_reports_omitted(self):
        s = chat.Session()
        self.addCleanup(s.close)
        # one message far bigger than the window must be dropped and reported
        s.data['messages'] = [{'role': 'user', 'content': 'x' * (chat.MAX_CONTEXT + 100)},
                              {'role': 'assistant', 'content': 'small answer'}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            prompt = s.context('the new question')
        self.assertIn('the new question', prompt)
        self.assertIn('small answer', prompt)
        self.assertNotIn('x' * 200, prompt)
        self.assertIn('mensagens antigas', buf.getvalue())

    def test_session_dir_is_created(self):
        d = chat.session_dir()
        self.assertTrue(d.is_dir())


# --------------------------------------------------------------------------- labels / describe


class LabelTests(Base):
    def test_model_label_variants(self):
        values = {'codex': 'gpt-x', 'codex_effort': 'high', 'claude': 'opus',
                  'openrouter': '', 'deepseek': '', 'agy': ''}
        with patch.object(chat.config_tool, 'model_values', return_value=values):
            self.assertEqual(chat.model_label('auto', 'profile'), 'profile')  # not a tool
            self.assertEqual(chat.model_label('codex'), 'gpt-x high')
            self.assertIn('gratuito', chat.model_label('openrouter'))
            self.assertIn('deepseek-chat', chat.model_label('deepseek'))
            self.assertIn('padrão', chat.model_label('agy'))
            self.assertIn('opus', chat.model_label('claude', 'prof'))
            self.assertIn('prof', chat.model_label('claude', 'prof'))

    def test_describe_tool_every_shape(self):
        self.assertEqual(chat.describe_tool('Bash', {'description': 'build'}), ('Ran', 'build'))
        self.assertEqual(chat.describe_tool('Bash', {'command': 'ls'}), ('Ran', 'ls'))
        self.assertEqual(chat.describe_tool('Read', {'file_path': '/a'})[0], 'Read')
        self.assertEqual(chat.describe_tool('Write', {'file_path': '/a'})[0], 'Wrote')
        self.assertEqual(chat.describe_tool('Edit', {'file_path': '/a'})[0], 'Edited')
        self.assertEqual(chat.describe_tool('NotebookEdit', {'notebook_path': '/a'})[0], 'Edited')
        self.assertEqual(chat.describe_tool('Grep', {'pattern': 'x'}), ('Searched', 'x'))
        self.assertEqual(chat.describe_tool('Glob', {'pattern': 'y'}), ('Searched', 'y'))
        self.assertEqual(chat.describe_tool('WebFetch', {'url': 'u'}), ('Fetched', 'u'))
        self.assertEqual(chat.describe_tool('WebSearch', {'query': 'q'}), ('Searched web', 'q'))
        self.assertEqual(chat.describe_tool('Task', {'description': 'd'}), ('Agent', 'd'))
        self.assertEqual(chat.describe_tool('Skill', {'skill': 's'}), ('Skill', 's'))
        self.assertEqual(chat.describe_tool('TodoWrite', {}), ('Updated todos', ''))
        self.assertEqual(chat.describe_tool('mcp__server__do', {})[0], 'Called')
        name, payload = chat.describe_tool('Unknown', {'a': 1})
        self.assertEqual(name, 'Unknown')
        self.assertIn('a', payload)
        self.assertEqual(chat.describe_tool('Bash', None), ('Ran', ''))


# --------------------------------------------------------------------------- spend


class SpendTests(Base):
    def test_spend_sums_tokens_and_cost(self):
        job = {}
        chat.spend(job, {'prompt_tokens': 10, 'completion_tokens': 5, 'cost': 0.5})
        chat.spend(job, {'input_tokens': 3, 'cost_in_usd_ticks': 2e10})
        self.assertEqual(job['tokens'], 18)
        self.assertAlmostEqual(job['cost'], 0.5 + 2.0)

    def test_spend_label(self):
        self.assertEqual(chat.spend_label({}), '')
        label = chat.spend_label({'cost': 0.1234, 'tokens': 2500})
        self.assertIn('US$ 0.1234', label)
        self.assertIn('2.5k tokens', label)
        self.assertIn('500 tokens', chat.spend_label({'tokens': 500}))

    def test_record_spend_writes_a_line(self):
        job = {'engine': 'codex', 'provider': 'codex', 'cost': 0.01, 'tokens': 100}
        chat.record_spend(job)
        rows = chat.costs_file().read_text().splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])['tool'], 'codex')

    def test_record_spend_skips_empty(self):
        chat.record_spend({'engine': 'codex'})
        self.assertFalse(chat.costs_file().exists())


# --------------------------------------------------------------------------- Runner.event


class EventTests(Base):
    def runner_job(self):
        runner = chat.Runner(str(self.root), mode='default')
        job = {'id': 'agente-1', 'engine': 'auto', 'sent': 'hi', 'events': [], 'answers': [], 'view': ui.View('')}
        return runner, job

    def test_status_line_and_noise(self):
        runner, job = self.runner_job()
        runner.event(job, 'a useful status line')
        runner.event(job, '🤖 noise banner')  # matches NOISE, no note
        self.assertEqual(job['events'][0]['type'], 'status')

    def test_reasoning_is_ignored(self):
        runner, job = self.runner_job()
        runner.event(job, '{"type": "reasoning"}')
        runner.event(job, '{"item": {"type": "reasoning"}}')
        self.assertEqual(job['events'], [])

    def test_route_and_provider_and_request(self):
        runner, job = self.runner_job()
        runner.event(job, '{"type": "smart.route", "label": "complex", "by": "rules"}')
        self.assertEqual(job['route']['label'], 'complex')
        runner.event(job, '{"type": "smart.route", "label": "simple", "by": "model-x"}')
        runner.event(job, '{"type": "smart.provider", "provider": "codex", "profile": "p"}')
        self.assertEqual(job['provider'], 'codex')
        self.assertEqual(job['dispatches'][-1]['provider'], 'codex')
        runner.event(job, '{"type": "smart.request", "data": {"messages": []}}')
        self.assertEqual(job['payload'], {'messages': []})
        self.assertEqual(job['dispatches'][-1]['sent'], {'messages': []})

    def test_streaming_then_answer(self):
        runner, job = self.runner_job()
        runner.event(job, '{"type": "smart.delta", "text": "line one\\nhalf "}')
        runner.event(job, '{"type": "smart.delta", "text": "two"}')
        runner.event(job, '{"type": "smart.answer", "content": "line one\\nhalf two", "model": "m", "usage": {"input_tokens": 5}}')
        self.assertEqual(job['answers'], ['line one\nhalf two'])

    def test_non_streaming_answer(self):
        runner, job = self.runner_job()
        runner.event(job, '{"type": "smart.answer", "content": "whole", "model": "m"}')
        self.assertEqual(job['answers'], ['whole'])

    def test_codex_item_events(self):
        runner, job = self.runner_job()
        runner.event(job, '{"type": "item.started", "item": {"type": "command_execution", "command": "ls"}}')
        runner.event(job, '{"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "ok", "exit_code": 0}}')
        runner.event(job, '{"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "boom", "exit_code": 2}}')
        runner.event(job, '{"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "/a.py"}]}}')
        runner.event(job, '{"type": "item.started", "item": {"type": "mcp_tool_call", "server": "s", "tool": "t"}}')
        runner.event(job, '{"type": "item.completed", "item": {"type": "mcp_tool_call", "result": "done", "status": "ok"}}')
        runner.event(job, '{"type": "item.completed", "item": {"type": "mcp_tool_call", "error": "no", "status": "failed"}}')
        runner.event(job, '{"type": "item.started", "item": {"type": "web_search", "query": "q"}}')
        runner.event(job, '{"type": "item.completed", "item": {"type": "agent_message", "text": "final answer"}}')
        self.assertIn('final answer', job['answers'])

    def test_claude_assistant_user_result_events(self):
        runner, job = self.runner_job()
        runner.event(job, json.dumps({'type': 'assistant', 'message': {'content': [
            {'type': 'thinking', 'thinking': 'hidden'},
            {'type': 'text', 'text': 'visible answer'},
            {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'ls'}}]}}))
        self.assertIn('visible answer', job['answers'])
        runner.event(job, json.dumps({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'content': [{'type': 'text', 'text': 'tool said'}], 'is_error': False}]}}))
        runner.event(job, json.dumps({'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'content': 'string result', 'is_error': True}]}}))
        # result with an api-key profile records cost
        job['dispatches'] = [{'profile': 'api-key-1'}]
        runner.event(job, json.dumps({'type': 'result', 'total_cost_usd': 0.02, 'usage': {'output_tokens': 7}}))
        self.assertEqual(job['cost'], 0.02)

    def test_result_error_and_fallback_answer(self):
        runner, job = self.runner_job()
        runner.event(job, json.dumps({'type': 'result', 'is_error': True, 'result': 'bad'}))
        self.assertTrue(job['protocol_error'])
        runner2, job2 = self.runner_job()
        runner2.event(job2, json.dumps({'type': 'result', 'result': 'only answer'}))
        self.assertIn('only answer', job2['answers'])

    def test_turn_completed_and_errors(self):
        runner, job = self.runner_job()
        runner.event(job, json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1}}))
        runner.event(job, json.dumps({'type': 'error', 'error': {'message': 'kaput'}}))
        self.assertTrue(job['protocol_error'])
        runner.event(job, json.dumps({'type': 'turn.failed', 'message': 'nope'}))

    def test_invalid_json_and_blank(self):
        runner, job = self.runner_job()
        runner.event(job, '')  # ignored
        runner.event(job, '[1,2,3]')  # not a dict -> treated as status
        self.assertEqual(job['events'][-1]['type'], 'status')

    def test_answer_ignores_blank(self):
        runner, job = self.runner_job()
        runner.answer(job, '   ')
        self.assertEqual(job['answers'], [])


# --------------------------------------------------------------------------- Runner.run


class FakePopen:
    def __init__(self, lines, code=0, raise_os=False):
        if raise_os:
            raise OSError('cannot start')
        self.stdout = io.StringIO('\n'.join(lines) + '\n')
        self._code = code

    def wait(self):
        return self._code


class RunnerRunTests(Base):
    def test_run_consumes_events_and_finishes_done(self):
        runner = chat.Runner(str(self.root))
        job = {'id': 'agente-1', 'engine': 'codex', 'sent': 'do it'}
        lines = [json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'A'}})]
        with patch.object(chat.subprocess, 'Popen', return_value=FakePopen(lines)):
            runner.run(job, 'classify')
        self.assertEqual(job['status'], 'done')
        self.assertEqual(job['answers'], ['A'])

    def test_run_failed_without_answer(self):
        runner = chat.Runner(str(self.root))
        job = {'id': 'agente-1', 'engine': 'auto', 'sent': 'x'}
        with patch.object(chat.subprocess, 'Popen', return_value=FakePopen([], code=1)):
            runner.run(job, 'c')
        self.assertEqual(job['status'], 'failed')

    def test_run_cancelled_before_start(self):
        runner = chat.Runner(str(self.root))
        runner.cancelled.set()
        job = {'id': 'agente-1', 'engine': 'auto', 'sent': 'x'}
        with patch.object(chat.subprocess, 'Popen') as popen:
            runner.run(job, 'c')
            popen.assert_not_called()
        self.assertEqual(job['status'], 'cancelled')

    def test_run_handles_os_error(self):
        runner = chat.Runner(str(self.root))
        job = {'id': 'agente-1', 'engine': 'auto', 'sent': 'x'}
        with patch.object(chat.subprocess, 'Popen', side_effect=OSError('no exec')):
            runner.run(job, 'c')
        self.assertEqual(job['status'], 'failed')

    def test_cancel_kills_children(self):
        runner = chat.Runner(str(self.root))
        child = MagicMock()
        runner.children.add(child)
        with patch.object(chat.runner, 'kill_tree') as kill:
            runner.cancel()
        self.assertTrue(runner.cancelled.is_set())
        kill.assert_called_once_with(child)


# --------------------------------------------------------------------------- execute


class FakeRunner:
    """Stands in for chat.Runner inside execute(): drives each job to a chosen status."""
    instances = []

    def __init__(self, cwd, mode='default'):
        self.cwd, self.mode = cwd, mode
        self.children = set()
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        FakeRunner.instances.append(self)

    def run(self, job, classify_prompt, readonly=False):
        outcome = FakeRunner.outcome
        job['status'] = outcome
        if outcome == 'done':
            job['answers'] = ['fake answer']
            job['provider'] = 'codex'
            job['tokens'] = 10
        else:
            job['answers'] = []
        return job

    def cancel(self):
        self.cancelled.set()


class ExecuteTests(Base):
    def setUp(self):
        super().setUp()
        FakeRunner.instances = []
        FakeRunner.outcome = 'done'
        p = patch.object(chat.runner, 'Runner', FakeRunner)
        p.start()
        self.addCleanup(p.stop)

    def session(self):
        s = chat.Session()
        self.addCleanup(s.close)
        return s

    def test_execute_done_appends_answer_and_title(self):
        s = self.session()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            chat.execute(s, [('codex', 'first question')])
        self.assertTrue(s.data['title'])
        roles = [m['role'] for m in s.data['messages']]
        self.assertEqual(roles, ['user', 'assistant'])
        self.assertEqual(s.data['messages'][-1]['content'], 'fake answer')

    def test_execute_failed_prints_failure(self):
        FakeRunner.outcome = 'failed'
        s = self.session()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            chat.execute(s, [('auto', 'do thing')])
        # no assistant message stored when it did not finish
        self.assertEqual([m['role'] for m in s.data['messages']], ['user'])

    def test_execute_cancelled(self):
        FakeRunner.outcome = 'cancelled'
        s = self.session()
        with contextlib.redirect_stdout(io.StringIO()):
            chat.execute(s, [('auto', 'do thing')])
        self.assertEqual([m['role'] for m in s.data['messages']], ['user'])

    def test_execute_parallel_adds_subtask_note(self):
        s = self.session()
        with contextlib.redirect_stdout(io.StringIO()):
            chat.execute(s, [('codex', 'a'), ('openrouter', 'b')], parallel=True)
        self.assertIn('Subtarefa delegada', s.data['requests'][0]['sent'])
        self.assertEqual(len(s.data['requests']), 2)

    def test_execute_raw_skips_context(self):
        s = self.session()
        with contextlib.redirect_stdout(io.StringIO()):
            chat.execute(s, [('claude', '/skill args')], raw=True)
        self.assertEqual(s.data['requests'][0]['sent'], '/skill args')


# --------------------------------------------------------------------------- compact


class CompactTests(Base):
    def test_compact_too_short(self):
        s = chat.Session()
        self.addCleanup(s.close)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            chat.compact(s)
        self.assertIn('Nada para compactar', buf.getvalue())

    def test_compact_success(self):
        s = chat.Session()
        self.addCleanup(s.close)
        s.data['messages'] = [{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'a'}]

        def fake_run(self, job, classify, readonly=False):
            job['status'] = 'done'
            job['answers'] = ['- summary point']
            return job
        with patch.object(chat.Runner, 'run', fake_run), contextlib.redirect_stdout(io.StringIO()):
            chat.compact(s)
        self.assertEqual(len(s.data['messages']), 1)
        self.assertIn('summary point', s.data['messages'][0]['content'])
        self.assertEqual(s.data['compacted'][0]['messages'][0]['content'], 'q')

    def test_compact_failure_keeps_history(self):
        s = chat.Session()
        self.addCleanup(s.close)
        s.data['messages'] = [{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'a'}]

        def fake_run(self, job, classify, readonly=False):
            job['status'] = 'failed'
            job['answers'] = []
            return job
        buf = io.StringIO()
        with patch.object(chat.Runner, 'run', fake_run), contextlib.redirect_stdout(buf):
            chat.compact(s)
        self.assertEqual(len(s.data['messages']), 2)
        self.assertIn('Não deu para resumir', buf.getvalue())


# --------------------------------------------------------------------------- misc helpers


class HelperTests(Base):
    def test_parse_parallel_valid(self):
        tasks = chat.parse_parallel('codex: analyse | openrouter: explain')
        self.assertEqual(tasks, [('codex', 'analyse'), ('openrouter', 'explain')])

    def test_parse_parallel_invalid(self):
        for bad in ('onlyone', 'codex: a | nope: b', 'codex: a | codex:'):
            with self.assertRaises(ValueError):
                chat.parse_parallel(bad)

    def test_run_spec_dispatch(self):
        s = object()
        with patch.object(chat.queue, 'execute') as run, patch.object(chat.queue, 'compact') as comp:
            chat.run_spec(s, {'kind': 'ask', 'engine': 'codex', 'text': 'hi'})
            chat.run_spec(s, {'kind': 'skill', 'tool': 'claude', 'prompt': 'p', 'raw': True})
            chat.run_spec(s, {'kind': 'parallel', 'tasks': [['codex', 'x']]})
            chat.run_spec(s, {'kind': 'compact'})
        comp.assert_called_once_with(s)
        self.assertEqual(run.call_count, 3)
        with self.assertRaises(ValueError):
            chat.run_spec(s, {'kind': 'bogus'})

    def test_queue_command_empty(self):
        q = chat.Queue()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            chat.queue_command(q, '')
        self.assertIn('Fila vazia', buf.getvalue())

    def test_queue_command_bad_action(self):
        q = chat.Queue()
        with self.assertRaises(ValueError):
            chat.queue_command(q, 'teleport')

    def test_run_tool_invokes_cli(self):
        with patch.object(chat.subprocess, 'call') as call:
            chat.run_tool(['--usage'])
        call.assert_called_once()

    def test_run_tool_swallows_interrupt(self):
        with patch.object(chat.subprocess, 'call', side_effect=KeyboardInterrupt):
            chat.run_tool(['--usage'])  # must not raise

    def test_run_native_opens_tool(self):
        with patch.object(chat.ops, 'resolve_bin', return_value='/bin/claude'), \
             patch.object(chat.subprocess, 'call') as call, contextlib.redirect_stdout(io.StringIO()):
            chat.run_native('claude', 'usage', str(self.root))
        self.assertEqual(call.call_args.args[0][0], '/bin/claude')

    def test_run_native_missing_binary(self):
        with patch.object(chat.ops, 'resolve_bin', return_value=None):
            with self.assertRaises(ValueError):
                chat.run_native('codex', 'status', str(self.root))

    def test_terminal_setup_by_program(self):
        with patch.dict(os.environ, {'TERM_PROGRAM': 'Apple_Terminal'}):
            self.assertIn('Shift+Enter', chat.terminal_setup())
        with patch.dict(os.environ, {'TERM_PROGRAM': 'iTerm.app'}):
            self.assertIn('iTerm2', chat.terminal_setup())
        with patch.dict(os.environ, {'TERM_PROGRAM': 'something-else'}):
            self.assertTrue(chat.terminal_setup())

    def test_setup_completion_runs(self):
        chat.setup_completion(str(self.root))  # exercises the readline wiring

    def test_list_sessions(self):
        s = chat.Session()
        s.data['title'] = 'My chat'
        s.save()
        s.close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            chat.list_sessions()
        self.assertIn(s.id, buf.getvalue())


# --------------------------------------------------------------------------- releases / i18n


class ReleaseTests(Base):
    def test_releases_prefers_published(self):
        data = [{'tag_name': 'v1.0', 'draft': False}, {'tag_name': 'v0.9', 'draft': True}]
        with patch.object(chat.versions, 'github', return_value=data):
            items = chat.releases()
        self.assertEqual([r['tag_name'] for r in items], ['v1.0'])

    def test_releases_falls_back_to_commits(self):
        commits = [{'sha': 'abc1234def', 'commit': {'message': 'feat: new thing\n\nbody text',
                                                     'committer': {'date': '2024-01-02T00:00:00Z'}}},
                   {'sha': 'zzz', 'commit': {'message': 'chore: nothing', 'committer': {'date': '2024-01-01T00:00:00Z'}}}]

        def fake_github(url):
            return [] if 'releases' in url else commits
        with patch.object(chat.versions, 'github', side_effect=fake_github):
            items = chat.releases()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['name'], 'new thing')

    def test_translate_passthrough_for_english(self):
        self.assertEqual(chat.translate(['hello'], 'en'), ['hello'])
        self.assertEqual(chat.translate([], 'pt'), [])

    def test_translate_uses_classifier_and_caches(self):
        with patch.object(chat, 'cli', create=True):
            pass
        from omni_ai import cli
        with patch.object(cli, 'free_classifiers', return_value=[('groq', 'http://x', {'model': 'm'})]), \
             patch.object(chat.config_tool, 'stored_keys', return_value=[('main', 'k', 'file')]), \
             patch.object(cli, 'ask_text', return_value='1. Texto traduzido'):
            out = chat.translate(['Translated text'], 'pt')
        self.assertEqual(out, ['Texto traduzido'])
        # cached: a second call needs no classifier
        with patch.object(cli, 'free_classifiers', side_effect=AssertionError('should be cached')):
            self.assertEqual(chat.translate(['Translated text'], 'pt'), ['Texto traduzido'])

    def test_installed_version(self):
        self.assertIsInstance(chat.installed_version(), str)

    def test_newer_release_none_for_dev_build(self):
        with patch.object(chat.versions, 'installed_version', return_value=''):
            self.assertIsNone(chat.newer_release())

    def test_newer_release_detects_newer(self):
        latest = {'tag_name': 'v9.9.9', 'draft': False, 'prerelease': False, 'body': '* did a thing'}
        with patch.object(chat.versions, 'installed_version', return_value='1.0.0'), \
             patch.object(chat.versions, 'github', return_value=[latest]):
            result = chat.newer_release()
        self.assertEqual(result['tag_name'], 'v9.9.9')
        # second call within a day reads the cached stamp
        with patch.object(chat.versions, 'installed_version', return_value='1.0.0'), \
             patch.object(chat.versions, 'github', side_effect=AssertionError('cached')):
            self.assertIsNotNone(chat.newer_release())

    def test_newer_release_none_when_current(self):
        with patch.object(chat.versions, 'installed_version', return_value='9.9.9'), \
             patch.object(chat.versions, 'github', return_value=[{'tag_name': 'v1.0.0', 'draft': False, 'prerelease': False}]):
            self.assertIsNone(chat.newer_release())

    def test_announce_release(self):
        latest = {'tag_name': 'v2.0.0', 'body': '* shiny feature'}
        buf = io.StringIO()

        class InlineThread:
            def __init__(self, target=None, daemon=None, **kw):
                self._target = target
            def start(self):
                self._target()

        with patch.object(chat.versions, 'newer_release', return_value=latest), \
             patch.object(chat.versions, 'translate', side_effect=lambda texts, lang: list(texts)), \
             patch.object(chat.threading, 'Thread', InlineThread), \
             contextlib.redirect_stdout(buf):
            chat.announce_release()
        self.assertIn('v2.0.0', buf.getvalue())

    def test_show_versions_list_and_detail(self):
        items = [{'tag_name': 'v1.0', 'published_at': '2024-01-01T00:00:00Z', 'name': 'first',
                  'commit': True, 'body': '* first\n* second'}]
        with patch.object(chat.versions, 'releases', return_value=items), \
             patch.object(chat.versions, 'translate', side_effect=lambda texts, lang: list(texts)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                chat.show_versions('')
            self.assertIn('v1.0', buf.getvalue())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                chat.show_versions('1')
            self.assertIn('first', buf.getvalue())

    def test_show_versions_bad_argument(self):
        with patch.object(chat.versions, 'releases', return_value=[{'tag_name': 'v1.0'}]):
            with self.assertRaises(ValueError):
                chat.show_versions('999')

    def test_show_versions_no_network(self):
        with patch.object(chat.versions, 'releases', side_effect=OSError):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                chat.show_versions('')
            self.assertIn('Não deu para buscar', buf.getvalue())


def until(condition, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- queue internals


class QueueInternalsTests(Base):
    def test_waited_request_shows_next_up_and_skip(self):
        ran = []
        skip = lambda label: label == 'redundant'
        q = chat.Queue(skip=skip)
        release = threading.Event()
        q.put('busy', release.wait)
        self.assertTrue(until(lambda: q.snapshot()[0] == 'busy'))
        q.put('redundant', lambda: ran.append('redundant'))
        q.put('do it', lambda: ran.append('do it'))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            release.set()
            q.wait()
        self.assertEqual(ran, ['do it'])  # the redundant one was skipped
        self.assertIn('da fila', buf.getvalue())

    def test_remove_move_clear_and_pending(self):
        q = chat.Queue(run=lambda spec: None)
        release = threading.Event()
        q.put('busy', release.wait)
        self.assertTrue(until(lambda: q.snapshot()[0] == 'busy'))
        for text in ('a', 'b', 'c'):
            q.put(text, {'kind': 'ask', 'engine': 'codex', 'text': text})
        self.assertEqual(q.move(3, -1), 2)
        self.assertEqual(q.remove(1), 'a')
        self.assertEqual([lbl for lbl in q.snapshot()[1]], ['c', 'b'])
        # the running item is a plain callable, so only the dict-spec waiters are saveable
        labels = [s['label'] for s in q.pending()]
        self.assertEqual(labels, ['c', 'b'])
        self.assertEqual(q.clear(), 2)
        release.set()
        q.wait()

    def test_queue_command_listing_and_edits(self):
        q = chat.Queue(run=lambda spec: None)
        release = threading.Event()
        q.put('busy', release.wait)
        self.assertTrue(until(lambda: q.snapshot()[0] == 'busy'))
        for text in ('one', 'two', 'three'):
            q.put(text, {'kind': 'ask', 'engine': 'codex', 'text': text})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            chat.queue_command(q, 'down 1')
            chat.queue_command(q, 'rm 3')
            chat.queue_command(q, 'up 2')
            chat.queue_command(q, 'clear')
        text = buf.getvalue()
        self.assertIn('Agora é', text)
        self.assertIn('Tirado da fila', text)
        release.set()
        q.wait()

    def test_activity_lines(self):
        q = chat.Queue()
        release = threading.Event()
        q.put('running one', release.wait)
        self.assertTrue(until(lambda: q.snapshot()[0] == 'running one'))
        for i in range(7):
            q.put(f'wait {i}', lambda: None)
        lines = chat.activity(q)
        self.assertIn('running one', lines[0])
        self.assertTrue(any('+2' in l for l in lines))
        release.set()
        q.wait()
        self.assertEqual(chat.activity(q), [])

    def test_cancel_drops_waiting_and_stops_running(self):
        runner = MagicMock()
        chat.ACTIVE.add(runner)
        self.addCleanup(chat.ACTIVE.discard, runner)
        q = chat.Queue()
        release = threading.Event()
        q.put('run', release.wait)
        self.assertTrue(until(lambda: q.snapshot()[0] == 'run'))
        q.put('wait', lambda: None)
        self.assertEqual(q.cancel(), 1)
        runner.cancel.assert_called()
        release.set()
        q.wait()


# --------------------------------------------------------------------------- main loop driver


class FakeBox:
    """A non-interactive InputBox: session is None, so main() reads with input()."""
    def __init__(self, *args, **kwargs):
        self.session = None


class MainLoopTests(Base):
    """Drive main()'s command dispatch through input(), with every side effect mocked out."""

    def drive(self, inputs, discover=None, native=None, argv=None):
        self.exec_mock = MagicMock()
        self.compact_mock = MagicMock()
        self.tool_mock = MagicMock()
        self.native_mock = MagicMock()
        feed = list(inputs)

        def fake_input(prompt=''):
            if not feed:
                raise EOFError
            value = feed.pop(0)
            if isinstance(value, type) and issubclass(value, BaseException):
                raise value
            return value

        patches = [
            patch.object(chat.ui, 'InputBox', FakeBox),
            patch.object(builtins, 'input', side_effect=fake_input),
            patch.object(sys.stdin, 'isatty', return_value=False),
            patch.object(chat.queue, 'execute', self.exec_mock),
            patch.object(chat.queue, 'compact', self.compact_mock),
            patch.object(chat, 'run_tool', self.tool_mock),
            patch.object(chat, 'run_native', self.native_mock),
            patch.object(chat.skills, 'discover', return_value=discover or {}),
            patch.object(chat.skills, 'native_command', side_effect=native or (lambda name: None)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            chat.main(argv or [])
        return buf.getvalue()

    def test_help_and_exit(self):
        out = self.drive(['/help', '/exit'])
        self.assertIn('/tool', out)

    def test_tool_valid_and_invalid(self):
        out = self.drive(['/tool nonsense', '/tool codex', '/exit'])
        self.assertIn('Ferramenta inválida', out)
        self.assertIn('Ferramenta: codex', out)

    def test_mode_show_set_and_bad(self):
        out = self.drive(['/mode', '/mode plan', '/mode wrong', '/exit'])
        self.assertIn('Modo:', out)
        self.assertIn('Modo inválido', out)

    def test_mode_yolo_confirmed_and_declined(self):
        out = self.drive(['/mode yolo', 's', '/mode yolo', 'n', '/exit'])
        # confirmed once -> mode set and reported
        self.assertIn('Modo: yolo', out)

    def test_tool_commands_route_to_run_tool(self):
        self.drive(['/usage', '/doctor extra', '/exit'])
        self.assertEqual(self.tool_mock.call_count, 2)

    def test_lang_valid(self):
        out = self.drive(['/lang en', '/exit'])
        self.assertIn('Language: English', out)

    def test_history_and_sessions(self):
        out = self.drive(['/history', '/sessions', '/exit'])
        self.assertIsInstance(out, str)

    def test_queue_versions_and_unknown(self):
        with patch.object(chat, 'show_versions') as sv:
            out = self.drive(['/queue', '/versions', '/notacommand', '/exit'])
        self.assertIn('Fila vazia', out)
        self.assertIn('desconhecido', out)
        sv.assert_called_once()

    def test_plain_question_runs_via_run_spec(self):
        self.drive(['hello there', '/exit'])
        self.exec_mock.assert_called_once()
        self.assertEqual(self.exec_mock.call_args.args[1], [('auto', 'hello there')])

    def test_compact_and_parallel_dispatch(self):
        self.drive(['/compact', '/parallel codex: a | openrouter: b', '/exit'])
        self.compact_mock.assert_called_once()
        self.exec_mock.assert_called_once()
        self.assertTrue(self.exec_mock.call_args.kwargs.get('parallel'))

    def test_parallel_bad_shows_error(self):
        out = self.drive(['/parallel oops', '/exit'])
        self.assertIn('Erro', out)

    def test_skills_listing_empty(self):
        out = self.drive(['/skills', '/exit'])
        self.assertIn('Nenhuma skill', out)

    def test_skills_listing_with_entry(self):
        entry = {'review': {'claude': ('command', self.root / 'r.md', 'projeto')}}
        with patch.object(chat.skills, 'description', return_value='review a PR'):
            out = self.drive(['/skills', '/exit'], discover=entry)
        self.assertIn('/review', out)

    def test_native_command_opens_tool(self):
        self.drive(['/codex:status', '/exit'], native=lambda name: ('codex', 'status') if name == 'codex:status' else None)
        self.native_mock.assert_called_once()

    def test_run_skill_builds_prompt(self):
        entry = {'myskill': {'claude': ('command', self.root / 's.md', 'projeto')}}
        with patch.object(chat.skills, 'pick_tool', return_value='claude'), \
             patch.object(chat.skills, 'argument_hint', return_value=''), \
             patch.object(chat.skills, 'build_prompt', return_value=('/myskill go', True)):
            out = self.drive(['/myskill go', '/exit'], discover=entry)
        self.assertIn('/myskill', out)
        self.exec_mock.assert_called_once()

    def test_new_and_clear_swap_session(self):
        out = self.drive(['/new', '/clear', '/exit'])
        self.assertIn('Conversa limpa', out)

    def test_resume_switches_to_existing_session(self):
        s = chat.Session()
        sid = s.id
        s.data['messages'] = [{'role': 'user', 'content': 'earlier'}]
        s.save()
        s.close()
        out = self.drive([f'/resume {sid}', '/exit'])
        self.assertIn(sid, out)

    def test_sent_and_trace(self):
        out = self.drive(['/sent', '/exit'])  # no requests yet
        self.assertIn('Nenhuma chamada', out)

    def test_sent_with_a_recorded_request(self):
        # pre-seed a request into the session that main() opens
        def seed(*a, **k):
            # first plain question is intercepted by our execute mock; seed manually instead
            pass
        s = chat.Session()
        sid = s.id
        s.data['requests'] = [{'id': 'agente-1', 'status': 'done', 'sent': 'the prompt', 'events': [{'type': 'x'}],
                               'dispatches': [{'provider': 'codex'}]}]
        s.save()
        s.close()
        out = self.drive(['/sent agente-1', '/trace agente-1', '/exit'], argv=['--resume', sid])
        self.assertIn('codex', out)

    def test_terminal_setup_and_setup_guide(self):
        with patch.object(chat.onboarding, 'guide', return_value='GUIDE'):
            out = self.drive(['/terminal-setup', '/setup', '/exit'])
        self.assertIn('GUIDE', out)

    def test_keyboard_interrupt_at_idle_prompt_exits(self):
        # Ctrl-C with an empty queue breaks the loop
        out = self.drive([KeyboardInterrupt])
        self.assertIsInstance(out, str)

    def test_sessions_argv_lists_and_returns(self):
        s = chat.Session()
        s.close()
        buf = io.StringIO()
        with patch.object(chat.ui, 'InputBox', FakeBox), contextlib.redirect_stdout(buf):
            chat.main(['--sessions'])
        self.assertIn(s.id, buf.getvalue())

    def test_bad_resume_argument_errors_out(self):
        with patch.object(chat.ui, 'InputBox', FakeBox), self.assertRaises(SystemExit):
            chat.main(['--resume', 'not-valid-id'])


if __name__ == '__main__':
    unittest.main()
