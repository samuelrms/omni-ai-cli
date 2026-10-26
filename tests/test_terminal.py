import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ui  # noqa: E402
import io
import contextlib
from terminal import Runner, Session, execute, parse_parallel

FAKE = '''#!/usr/bin/env python3
import json, os, sys, time
with open(os.environ['TEST_CALLS'], 'a') as out:
    out.write(json.dumps({'args': sys.argv[1:], 'start': time.time()}) + '\\n')
print(json.dumps({'type':'thread.started', 'thread_id':'fake'}), flush=True)
print(json.dumps({'type':'item.started', 'item':{'type':'command_execution','command':'rg example'}}), flush=True)
time.sleep(float(os.environ.get('TEST_DELAY', '0')))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'FAKE ANSWER'}}), flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1}}), flush=True)
'''


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        executable = self.root / 'codex'
        executable.write_text(FAKE)
        executable.chmod(0o700)
        self.env = patch.dict(os.environ, {
            'SMART_AI_SESSIONS': str(self.root/'sessions'), 'SMART_AI_STATE': str(self.root/'state'),
            'SMART_AI_LOG': str(self.root/'usage'), 'CODEX_BIN': str(executable),
            'TEST_CALLS': str(self.root/'calls'), 'TEST_DELAY': '0',
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_session_resume_history_and_permissions(self):
        session = Session()
        session.data['messages'].append({'role': 'user', 'content': 'Remember ALPHA'})
        session.save()
        self.assertEqual(session.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, 'já aberta'):
            Session(session.id)
        session.close()
        resumed = Session(session.id)
        self.addCleanup(resumed.close)
        self.assertIn('ALPHA', resumed.context('next'))

    def test_chat_entrypoint_and_followup_context(self):
        result = subprocess.run([sys.executable, str(ROOT/'smart_ai.py'), '--chat'],
                                input='/tool codex\nRemember ALPHA\nWhat was it?\n/sent\n/exit\n',
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('◆ codex', result.stdout)
        self.assertIn('● Ran rg example', result.stdout)
        self.assertNotIn('[agente-', result.stdout)
        calls = [json.loads(row) for row in (self.root/'calls').read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertIn('ALPHA', calls[1]['args'][-1])
        self.assertIn('FAKE ANSWER', calls[1]['args'][-1])
        saved = json.loads(next((self.root/'sessions').glob('*.json')).read_text())
        self.assertEqual(len(saved['messages']), 4)
        self.assertEqual(saved['requests'][-1]['status'], 'done')
        self.assertEqual(saved['requests'][-1]['dispatches'][0]['provider'], 'codex')

    def test_parallel_real_processes_overlap_and_are_read_only(self):
        session = Session()
        self.addCleanup(session.close)
        with patch.dict(os.environ, TEST_DELAY='0.7'):
            execute(session, [('codex','first'), ('codex','second')], parallel=True)
        calls = [json.loads(row) for row in (self.root/'calls').read_text().splitlines()]
        self.assertLess(abs(calls[0]['start'] - calls[1]['start']), 0.5)
        for call in calls:
            self.assertIn('--sandbox', call['args'])
            self.assertIn('read-only', call['args'])
        self.assertEqual([j['status'] for j in session.data['requests']], ['done', 'done'])
        self.assertEqual(len(session.data['messages']), 3)

    def test_cancel_terminates_process_group(self):
        runner = Runner(str(self.root))
        job = {'id':'test', 'engine':'codex', 'sent':'wait', 'status':'running'}
        with patch.dict(os.environ, TEST_DELAY='30'):
            thread = threading.Thread(target=runner.run, args=(job, 'wait'))
            thread.start()
            deadline = time.monotonic() + 5
            while not (self.root/'calls').exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            runner.cancel()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(job['status'], 'cancelled')

    def test_model_events_and_internal_reasoning_not_exposed(self):
        runner = Runner(str(self.root))
        job = {'id':'test', 'events':[], 'answers':[], 'sent':'hello', 'view': ui.View()}
        runner.event(job, json.dumps({'type':'item.completed','item':{'type':'reasoning','text':'hidden'}}))
        runner.event(job, json.dumps({'type':'assistant','message':{'content':[{'type':'thinking','thinking':'hidden'}, {'type':'text','text':'answer'}]}}))
        self.assertNotIn('hidden', json.dumps({k: v for k, v in job.items() if k != 'view'}))
        self.assertEqual(job['answers'], ['answer'])
        runner.event(job, json.dumps({'type':'result','is_error':True}))
        self.assertTrue(job['protocol_error'])

    def test_compact_tool_lines_and_noise_hidden(self):
        runner = Runner(str(self.root))
        job = {'id':'test', 'events':[], 'answers':[], 'sent':'x', 'view': ui.View()}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.event(job, json.dumps({'type':'system','subtype':'init'}))
            runner.event(job, '─────────────────────────────────────')
            runner.event(job, '2026-10-26T21:40:06.865100Z ERROR rmcp::transport::worker: closed')
            runner.event(job, json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Bash',
                         'input':{'command':'gh pr view 240 --json x','description':'Find board card'}}]}}))
            runner.event(job, json.dumps({'type':'user','message':{'content':[{'type':'tool_result','content':'a\nb\nc'}]}}))
            runner.event(job, json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use',
                         'name':'mcp__obsidian__read_file','input':{'path':'x'}}]}}))
        text = out.getvalue()
        self.assertIn('● Ran Find board card', text)
        self.assertIn('└ a', text)
        self.assertIn('+ 2 linhas', text)
        self.assertIn('● Called obsidian · read_file', text)
        self.assertNotIn('system', text)
        self.assertNotIn('rmcp', text)
        self.assertNotIn('───', text)
        self.assertEqual(len(job['events']), 6)

    def test_parallel_rejects_unsupported_provider_and_invalid_shape(self):
        with self.assertRaises(ValueError):
            parse_parallel('claude: edit | codex: inspect')
        with self.assertRaises(ValueError):
            parse_parallel('codex: only one')
        self.assertEqual(len(parse_parallel('codex: inspect | openrouter: explain')), 2)

    def test_invalid_session_path_rejected(self):
        with self.assertRaises(ValueError):
            Session('../../credentials')

    def test_interrupted_job_recovered_on_resume(self):
        session = Session()
        session.data['requests'].append({'status':'running'})
        session.save()
        session.close()
        resumed = Session(session.id)
        self.addCleanup(resumed.close)
        self.assertEqual(resumed.data['requests'][0]['status'], 'interrupted')

if __name__ == '__main__':
    unittest.main()
