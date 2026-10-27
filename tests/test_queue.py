import json
import os
from pathlib import Path
import re
import select
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)
sys.path.insert(0, str(ROOT))
from omni_ai import chat, cli  # noqa: E402
from omni_ai.chat import Queue, Session, activity, already_answered, execute  # noqa: E402

# Fake Codex: logs each call, waits TEST_DELAY seconds, answers
FAKE = '''#!/usr/bin/env python3
import json, os, sys, time
with open(os.environ['TEST_CALLS'], 'a') as out:
    out.write(json.dumps({'prompt': sys.argv[-1], 'start': time.time()}) + '\\n')
time.sleep(float(os.environ.get('TEST_DELAY', '0')))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'FAKE ANSWER'}}), flush=True)
'''


def until(condition, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


class QueueTests(unittest.TestCase):
    def test_runs_in_order_one_at_a_time(self):
        queue, log, running = Queue(), [], []

        def work(name):
            running.append(name)
            log.append(('start', name, len(running)))
            time.sleep(0.05)
            running.remove(name)
            log.append(('end', name))
        for name in ('a', 'b', 'c'):
            queue.put(name, lambda name=name: work(name))
        queue.wait()
        self.assertFalse(queue.busy())
        self.assertEqual([entry[1] for entry in log if entry[0] == 'start'], ['a', 'b', 'c'])
        # never two at once
        self.assertTrue(all(entry[2] == 1 for entry in log if entry[0] == 'start'))

    def test_snapshot_and_activity_show_running_and_waiting(self):
        queue, release = Queue(), threading.Event()
        queue.put('first question', release.wait)
        self.assertTrue(until(lambda: queue.snapshot()[0] == 'first question'))
        for i in range(7):
            queue.put(f'waiting {i}\nsecond line', lambda: None)
        current, waiting = queue.snapshot()
        self.assertEqual(len(waiting), 7)
        lines = activity(queue)
        self.assertIn('first question', lines[0])
        self.assertIn('(7)', lines[1])
        self.assertIn('1. waiting 0', lines[2])
        self.assertNotIn('second line', ''.join(lines))  # only the first line of each request
        self.assertIn('+2', lines[-1])  # five listed, the rest counted
        release.set()
        queue.wait()
        self.assertEqual(activity(queue), [])

    def test_activity_escapes_markup(self):
        queue, release = Queue(), threading.Event()
        queue.put('<b>&', release.wait)
        self.assertTrue(until(queue.busy))
        self.assertTrue(until(lambda: queue.snapshot()[0] is not None))
        self.assertIn('&lt;b&gt;&amp;', activity(queue)[0])
        release.set()
        queue.wait()

    def test_only_requests_that_waited_can_be_skipped(self):
        checked, ran = [], []

        def skip(label):
            checked.append(label)
            return label == 'redundant'
        queue, release = Queue(skip=skip), threading.Event()
        queue.put('redundant', lambda: (release.wait(), ran.append('first')))
        queue.put('redundant', lambda: ran.append('second'))
        queue.put('new', lambda: ran.append('third'))
        release.set()
        queue.wait()
        # the first one ran straight away, so it was never checked
        self.assertEqual(checked, ['redundant', 'new'])
        self.assertEqual(ran, ['first', 'third'])

    def test_a_failing_request_does_not_stop_the_queue(self):
        queue, ran = Queue(), []
        queue.put('broken', lambda: 1 / 0)
        queue.put('fine', lambda: ran.append('fine'))
        queue.wait()
        self.assertEqual(ran, ['fine'])

    def test_cancel_drops_waiting_and_stops_running(self):
        queue, release, ran = Queue(), threading.Event(), []

        class FakeRunner:
            def cancel(self):
                release.set()
        runner = FakeRunner()
        chat.ACTIVE.add(runner)
        self.addCleanup(chat.ACTIVE.discard, runner)
        queue.put('running', release.wait)
        queue.put('waiting 1', lambda: ran.append(1))
        queue.put('waiting 2', lambda: ran.append(2))
        self.assertTrue(until(lambda: queue.snapshot()[0] == 'running'))
        self.assertEqual(queue.cancel(), 2)
        queue.wait()
        self.assertEqual(ran, [])
        self.assertFalse(queue.busy())


class AlreadyAnsweredTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        backends = patch.object(cli, 'free_classifiers', return_value=[('groq', 'https://example.invalid', {'model': 'm'})])
        keys = patch.object(chat.config_tool, 'stored_keys', return_value=[('principal', 'gsk_test', 'file')])
        for item in (backends, keys):
            item.start()
            self.addCleanup(item.stop)

    def verdict(self, reply):
        def ask(url, key, data, labels):
            self.sent.append(data)
            self.assertEqual(labels, ('yes', 'no'))
            return reply
        with patch.object(cli, 'ask_classifier', side_effect=ask):
            return already_answered('what about joao@example.com?', 'The answer for joao@example.com')

    def test_yes_skips_no_runs(self):
        self.assertTrue(self.verdict('yes'))
        self.assertFalse(self.verdict('no'))

    def test_no_backend_answer_means_run(self):
        self.assertFalse(self.verdict(None))

    def test_personal_data_is_masked_before_sending(self):
        self.verdict('no')
        self.assertNotIn('joao@example.com', json.dumps(self.sent))


class QueueWithRealProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        executable = self.root / 'codex'
        executable.write_text(FAKE)
        executable.chmod(0o700)
        self.env = {
            'SMART_AI_SESSIONS': str(self.root / 'sessions'), 'SMART_AI_STATE': str(self.root / 'state'),
            'SMART_AI_LOG': str(self.root / 'usage'), 'CODEX_BIN': str(executable),
            'TEST_CALLS': str(self.root / 'calls'), 'TEST_DELAY': '0',
        }
        patcher = patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def calls(self):
        path = self.root / 'calls'
        return [json.loads(row) for row in path.read_text().splitlines()] if path.exists() else []

    def test_queued_requests_reach_the_tool_in_order_with_context(self):
        session = Session()
        self.addCleanup(session.close)
        queue = Queue()
        for text in ('Remember ALPHA', 'What was it?'):
            queue.put(text, lambda text=text: execute(session, [('codex', text)]))
        queue.wait(timeout=20)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertLessEqual(calls[0]['start'], calls[1]['start'])
        # the second one sees the first answer: it ran after it finished
        self.assertIn('FAKE ANSWER', calls[1]['prompt'])
        self.assertEqual([r['status'] for r in session.data['requests']], ['done', 'done'])

    def test_session_saves_while_a_job_is_running(self):
        # the job dict holds its screen view until it finishes; saving then used to crash
        session = Session()
        self.addCleanup(session.close)
        session.data['requests'].append({'id': 'agente-1', 'status': 'running', 'view': object()})
        session.save()
        saved = json.loads(session.path.read_text())
        self.assertIsNone(saved['requests'][0]['view'])

    def test_cancel_kills_the_running_tool(self):
        os.environ['TEST_DELAY'] = '30'
        session = Session()
        self.addCleanup(session.close)
        queue = Queue()
        queue.put('slow', lambda: execute(session, [('codex', 'slow')]))
        queue.put('never', lambda: execute(session, [('codex', 'never')]))
        self.assertTrue(until(lambda: len(self.calls()) == 1 and chat.ACTIVE))
        started = time.time()
        self.assertEqual(queue.cancel(), 1)
        queue.wait(timeout=10)
        self.assertLess(time.time() - started, 8)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(session.data['requests'][-1]['status'], 'cancelled')


@unittest.skipIf(os.name == 'nt', 'needs a pseudo-terminal')
class ChatQueueTerminalTests(unittest.TestCase):
    """The real chat in a pseudo-terminal: typing while a request runs."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        executable = self.root / 'codex'
        executable.write_text(FAKE)
        executable.chmod(0o700)
        self.env = dict(os.environ, SMART_AI_SESSIONS=str(self.root / 'sessions'), SMART_AI_STATE=str(self.root / 'state'),
                        SMART_AI_LOG=str(self.root / 'usage'), CODEX_BIN=str(executable), TEST_CALLS=str(self.root / 'calls'),
                        TEST_DELAY='1.5', TERM='xterm-256color', COLUMNS='100', LINES='40', XDG_CONFIG_HOME=str(self.root / 'config'))
        (self.root / 'config').mkdir()

    def run_chat(self, lines, wait=6):
        import pty
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(sys.executable, [sys.executable, '-m', 'omni_ai.chat'], self.env)
        output = b''

        def pump(seconds):
            nonlocal output
            end = time.time() + seconds
            while time.time() < end:
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    try:
                        output += os.read(fd, 65536)
                    except OSError:
                        return
        pump(3)
        for line in lines:
            os.write(fd, line.encode() + b'\r')
            pump(0.3)
        pump(wait)
        os.write(fd, b'\x04')
        pump(2)
        # never hang the suite: a chat that did not exit is killed and reported
        exited = until(lambda: os.waitpid(pid, os.WNOHANG)[0] == pid, timeout=5)
        if not exited:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        os.close(fd)
        self.exited = exited
        return re.sub(r'\x1b\[[0-9;?>]*[a-zA-Z]|\x1b\][^\x07]*\x07', '', output.decode('utf-8', 'replace'))

    def test_messages_typed_while_busy_are_queued_and_run(self):
        text = self.run_chat(['/tool codex', 'first', 'second', '/clear'])
        calls = [json.loads(row) for row in (self.root / 'calls').read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(calls[1]['start'] - calls[0]['start'], 1.4)  # second waited for the first
        self.assertIn('first', text)
        self.assertRegex(text, r'na fila|queued|en cola|file')  # queue shown above the input
        self.assertRegex(text, r'Espere a fila|Wait for the queue|Espera a que|Attendez')  # /clear blocked while busy
        self.assertRegex(text, r'da fila: second|from the queue: second|de la cola: second|de la file : second')


if __name__ == '__main__':
    unittest.main()
