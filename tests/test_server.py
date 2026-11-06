"""`ai serve` end to end: a real server process, the real router, fictional CLIs."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from omni_ai.server import protocol  # noqa: E402

CODEX = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
pwd >> "$FAKE_LOG"
env | grep -c SMART_AI_SERVER_KEY >> "$FAKE_LOG"
case "$*" in
  *sleepforever*) echo $$ > "$FAKE_PID"; exec sleep 60 ;;
  *failnow*) echo "provider error"; exit 1 ;;
  *longanswer*) echo '{"type":"item.completed","item":{"type":"agent_message","text":"abcdefghijklmnopqrstuvwxyz"}}'; exit 0 ;;
esac
echo '{"type":"item.completed","item":{"type":"agent_message","text":"thinking out loud"}}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"codex answer"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":11,"output_tokens":7}}'
"""
CLAUDE = """#!/bin/sh
echo "claude $*" >> "$FAKE_LOG"
echo "You hit your usage limit, resets at 7pm"
exit 1
"""
OPENROUTER_KEY = 'sk-or-synthetic'


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = root = Path(cls.tmp.name)
        for name, body in (('codex', CODEX), ('claude', CLAUDE)):
            path = root / name
            path.write_text(body)
            path.chmod(0o700)
        cls.log, cls.pid_file = root / 'calls', root / 'pid'
        (root / 'profiles.json').write_text(json.dumps({'profiles': {'main': {'priority': 1, 'enabled': True}}}))
        cls.env = {**{k: v for k, v in os.environ.items() if not k.endswith('_API_KEY')},
                   'HOME': str(root), 'PYTHONPATH': str(ROOT), 'SMART_AI_HOME': str(root / 'cfg'),
                   'SMART_AI_CONFIG': str(root / 'profiles.json'), 'SMART_AI_STATE': str(root / 'state'),
                   'SMART_AI_LOG': str(root / 'usage.log'), 'CODEX_BIN': str(root / 'codex'),
                   'CLAUDE_BIN': str(root / 'claude'), 'AGY_BIN': str(root / 'none'), 'CURSOR_BIN': str(root / 'none'),
                   'GROK_BIN': str(root / 'none'), 'FAKE_LOG': str(cls.log), 'FAKE_PID': str(cls.pid_file)}
        cls.env.pop('SMART_AI_SERVER_KEY', None)
        cls.server, cls.port, cls.key = cls.start()

    @classmethod
    def start(cls, *args, env=None):
        process = subprocess.Popen([sys.executable, '-m', 'omni_ai.cli', 'serve', '--port', '0', *args],
                                   env=env or cls.env, cwd=cls.root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        port = int(re.search(r':(\d+)/v1', process.stdout.readline()).group(1))
        key = None
        if (env or cls.env).get('SMART_AI_SERVER_KEY') is None and not getattr(cls, 'key', None):
            process.stdout.readline()
            key = process.stdout.readline().strip()
        return process, port, key

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.server.stdout.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.log.write_text('')
        (self.root / 'state').unlink(missing_ok=True)

    def call(self, method, path, body=None, key=None, port=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', port or self.port, timeout=30)
        self.addCleanup(connection.close)
        sent = {'Authorization': f'Bearer {key or self.key}', **(headers or {})}
        connection.request(method, path, json.dumps(body) if isinstance(body, dict) else body, sent)
        response = connection.getresponse()
        return response, response.read().decode('utf-8')

    def chat(self, content='hello', model='omni/codex', **extra):
        return self.call('POST', '/v1/chat/completions',
                         {'model': model, 'messages': [{'role': 'user', 'content': content}], **extra})

    def events(self, text):
        return [line[6:] for line in text.split('\n') if line.startswith('data: ')]


class Authentication(ServerCase):
    def test_key_is_stored_only_as_a_hash(self):
        stored = (self.root / 'cfg' / 'server_keys.json').read_text()
        self.assertTrue(self.key.startswith('omni-'))
        self.assertNotIn(self.key, stored)
        self.assertEqual((self.root / 'cfg' / 'server_keys.json').stat().st_mode & 0o777, 0o600)

    def test_requests_without_a_valid_key_are_refused(self):
        response, text = self.call('GET', '/v1/models', key='wrong')
        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(text)['error']['type'], 'authentication_error')
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request('POST', '/v1/chat/completions', '{}')
        self.assertEqual(connection.getresponse().status, 401)
        self.assertEqual(self.log.read_text(), '')

    def test_keys_can_be_added_and_revoked_while_running(self):
        def keys(*args):
            return subprocess.run([sys.executable, '-m', 'omni_ai.cli', 'serve', 'keys', *args], env=self.env,
                                  capture_output=True, text=True)
        created = keys('add', 'app').stdout.strip().splitlines()[-1]
        self.assertEqual(self.call('GET', '/v1/models', key=created)[0].status, 200)
        self.assertIn('app', keys('list').stdout)
        self.assertNotEqual(keys('add', 'app').returncode, 0)
        self.assertEqual(keys('remove', 'app').returncode, 0)
        self.assertEqual(self.call('GET', '/v1/models', key=created)[0].status, 401)
        self.assertNotEqual(keys('remove', 'app').returncode, 0)

    def test_key_from_the_environment_works_and_no_key_is_generated(self):
        home = self.root / 'cfg-env'
        env = dict(self.env, SMART_AI_HOME=str(home), SMART_AI_SERVER_KEY='from-env')
        process, port, _ = self.start(env=env)
        try:
            self.assertEqual(self.call('GET', '/v1/models', key='from-env', port=port)[0].status, 200)
            self.assertEqual(self.call('GET', '/v1/models', key='wrong', port=port)[0].status, 401)
            self.assertFalse((home / 'server_keys.json').exists())
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdout.close()


class Models(ServerCase):
    def test_lists_auto_and_configured_tools_only(self):
        response, text = self.call('GET', '/v1/models')
        body = json.loads(text)
        ids = [item['id'] for item in body['data']]
        self.assertEqual(response.status, 200)
        self.assertEqual(body['object'], 'list')
        self.assertEqual(ids[0], 'omni-auto')
        self.assertIn('omni/codex', ids)
        self.assertNotIn('omni/cursor', ids)
        self.assertNotIn('omni/openrouter', ids)

    def test_single_model_and_unknown_model(self):
        self.assertEqual(json.loads(self.call('GET', '/v1/models/omni-auto')[1])['id'], 'omni-auto')
        self.assertEqual(self.call('GET', '/v1/models/gpt-4')[0].status, 404)
        response, text = self.chat(model='gpt-4')
        self.assertEqual(response.status, 404)
        self.assertEqual(json.loads(text)['error']['code'], 'model_not_found')


class Completions(ServerCase):
    def test_response_format_and_provider_header(self):
        response, text = self.chat()
        body = json.loads(text)
        self.assertEqual(response.status, 200)
        self.assertEqual(body['object'], 'chat.completion')
        self.assertEqual(body['model'], 'omni/codex')
        self.assertEqual(body['choices'][0]['message'], {'role': 'assistant', 'content': 'codex answer'})
        self.assertEqual(body['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(body['usage'], {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18})
        self.assertEqual(response.getheader('X-Omni-Provider'), 'codex')
        self.assertEqual(response.getheader('X-Omni-Usage'), 'reported')

    def test_tools_run_read_only_in_a_temporary_folder_without_the_server_key(self):
        env = dict(self.env, SMART_AI_SERVER_KEY='from-env', SMART_AI_MODE='yolo')
        process, port, _ = self.start(env=env)
        try:
            self.call('POST', '/v1/chat/completions', {'model': 'omni/codex', 'messages': [{'role': 'user', 'content': 'x'}]},
                      key='from-env', port=port)
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdout.close()
        args, folder, leaked = self.log.read_text().splitlines()[:3]
        self.assertIn('--sandbox read-only', args)
        self.assertNotIn('dangerously', args)
        self.assertIn('omni-serve-', folder)
        self.assertFalse(Path(folder).exists())
        self.assertEqual(leaked, '0')

    def test_fallback_is_transparent(self):
        response, text = self.chat(model='omni/claude')
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(text)['choices'][0]['message']['content'], 'codex answer')
        self.assertEqual(response.getheader('X-Omni-Provider'), 'codex')
        self.assertIn('claude', self.log.read_text())

    def test_failure_uses_openai_error_format_and_hides_details(self):
        response, text = self.chat('failnow')
        body = json.loads(text)
        self.assertEqual(response.status, 502)
        self.assertEqual(body['error']['type'], 'api_error')
        self.assertNotIn('provider error', text)

    def test_max_tokens_cuts_cli_answers(self):
        response, text = self.chat('longanswer', max_tokens=2)
        body = json.loads(text)
        self.assertEqual(body['choices'][0]['message']['content'], 'abcdefgh')
        self.assertEqual(body['choices'][0]['finish_reason'], 'length')
        self.assertEqual(response.getheader('X-Omni-Usage'), 'estimated')

    def test_ignored_parameters_are_reported(self):
        response, _ = self.chat(temperature=0.2, top_p=0.5)
        self.assertEqual(response.getheader('X-Omni-Ignored-Params'), 'temperature, top_p')

    def test_invalid_requests(self):
        cases = [
            ({'messages': [{'role': 'user', 'content': 'x'}]}, 'model'),
            ({'model': 'omni/codex', 'messages': []}, 'messages'),
            ({'model': 'omni/codex', 'messages': [{'role': 'tool', 'content': 'x'}]}, 'messages[0].role'),
            ({'model': 'omni/codex', 'messages': [{'role': 'user', 'content': 'x'}], 'tools': [{}]}, 'tools[0]'),
            ({'model': 'omni/codex', 'messages': [{'role': 'user', 'content': 'x'}], 'n': 2}, 'n'),
            ({'model': 'omni/codex', 'messages': [{'role': 'user', 'content': 'x'}], 'temperature': 3}, 'temperature'),
            ({'model': 'omni/codex', 'messages': [{'role': 'user', 'content': 'x'}], 'max_tokens': 0}, 'max_tokens'),
            ({'model': 'omni/codex', 'messages': [{'role': 'user', 'content': [{'type': 'image_url'}]}]}, 'messages[0].content'),
        ]
        for body, param in cases:
            response, text = self.call('POST', '/v1/chat/completions', body)
            self.assertEqual(response.status, 400, body)
            self.assertEqual(json.loads(text)['error']['param'], param)
        self.assertEqual(self.call('POST', '/v1/chat/completions', 'not json')[0].status, 400)
        self.assertEqual(self.log.read_text(), '')

    def test_body_limits_and_unknown_routes(self):
        self.assertEqual(self.call('POST', '/v1/chat/completions', '{}', headers={'Content-Length': '2000000'})[0].status, 413)
        self.assertEqual(self.call('POST', '/v1/nope', {})[0].status, 404)
        self.assertEqual(self.call('GET', '/nope')[0].status, 404)
        response, text = self.chat('x' * (protocol.MAX_PROMPT_BYTES + 1))
        self.assertEqual(json.loads(text)['error']['code'], 'context_length_exceeded')


class Streaming(ServerCase):
    def test_chunks_finish_and_done(self):
        response, text = self.chat(stream=True, stream_options={'include_usage': True})
        self.assertEqual(response.getheader('Content-Type'), 'text/event-stream')
        self.assertEqual(response.getheader('X-Omni-Provider'), 'codex')
        events = self.events(text)
        self.assertEqual(events[-1], '[DONE]')
        chunks = [json.loads(item) for item in events[:-1]]
        self.assertTrue(all(chunk['object'] == 'chat.completion.chunk' for chunk in chunks))
        self.assertEqual(chunks[0]['choices'][0]['delta']['role'], 'assistant')
        self.assertEqual(''.join(c['choices'][0]['delta'].get('content', '') for c in chunks if c['choices']), 'codex answer')
        self.assertEqual(chunks[-2]['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(chunks[-1]['choices'], [])
        self.assertEqual(chunks[-1]['usage']['total_tokens'], 18)

    def test_fallback_before_the_first_text_is_invisible(self):
        response, text = self.chat(model='omni/claude', stream=True)
        self.assertEqual(response.status, 200)
        self.assertNotIn('usage limit', text)
        self.assertIn('codex answer', text)

    def test_failure_before_the_first_text_keeps_the_http_status(self):
        response, text = self.chat('failnow', stream=True)
        self.assertEqual(response.status, 502)
        self.assertEqual(json.loads(text)['error']['type'], 'api_error')


class Stopping(ServerCase):
    def wait_for_child(self):
        self.pid_file.unlink(missing_ok=True)

    def pid(self):
        for _ in range(100):
            if self.pid_file.exists() and self.pid_file.read_text().strip():
                return int(self.pid_file.read_text())
            time.sleep(0.1)
        self.fail('the tool did not start')

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def assert_stops(self, pid, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline and self.alive(pid):
            time.sleep(0.1)
        self.assertFalse(self.alive(pid), 'the tool kept running')

    def raw_request(self, port=None):
        body = json.dumps({'model': 'omni/codex', 'messages': [{'role': 'user', 'content': 'sleepforever'}]})
        connection = socket.create_connection(('127.0.0.1', port or self.port), timeout=30)
        connection.sendall((f'POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {self.key}\r\n'
                            f'Content-Length: {len(body)}\r\n\r\n{body}').encode())
        return connection

    def test_client_disconnect_stops_the_tool(self):
        self.wait_for_child()
        connection = self.raw_request()
        pid = self.pid()
        connection.close()
        self.assert_stops(pid)

    def test_timeout_stops_the_tool_and_returns_504(self):
        self.wait_for_child()
        process, port, _ = self.start('--timeout', '1')
        try:
            response, text = self.call('POST', '/v1/chat/completions',
                                       {'model': 'omni/codex', 'messages': [{'role': 'user', 'content': 'sleepforever'}]}, port=port)
            self.assertEqual(response.status, 504)
            self.assert_stops(self.pid())
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdout.close()

    def test_full_server_answers_429_with_retry_after(self):
        self.wait_for_child()
        process, port, _ = self.start('--max-concurrent', '1')
        try:
            connection = self.raw_request(port)
            pid = self.pid()
            response, text = self.call('POST', '/v1/chat/completions',
                                       {'model': 'omni/codex', 'messages': [{'role': 'user', 'content': 'x'}]}, port=port)
            self.assertEqual(response.status, 429)
            self.assertEqual(response.getheader('Retry-After'), '1')
            self.assertEqual(json.loads(text)['error']['code'], 'server_busy')
            connection.close()
            self.assert_stops(pid)
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdout.close()

    @unittest.skipIf(os.name == 'nt', 'POSIX signal')
    def test_shutdown_stops_running_tools(self):
        self.wait_for_child()
        process, port, _ = self.start()
        connection = self.raw_request(port)
        self.addCleanup(connection.close)
        pid = self.pid()
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=15), 0)
        process.stdout.close()
        self.assert_stops(pid)


class CommandLine(ServerCase):
    def test_prose_starting_with_serve_stays_a_prompt(self):
        out = subprocess.run([sys.executable, '-m', 'omni_ai.cli', '--codex', 'serve', 'dinner'], env=self.env,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertIn('serve dinner', self.log.read_text())

    def test_busy_port_fails_cleanly(self):
        out = subprocess.run([sys.executable, '-m', 'omni_ai.cli', 'serve', '--port', str(self.port)], env=self.env,
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 1)
        self.assertNotIn('Traceback', out.stderr)


class Prompt(unittest.TestCase):
    def test_lone_user_message_is_untouched(self):
        self.assertEqual(protocol.build_prompt([{'role': 'user', 'content': 'hi'}]), ('hi', 'hi'))

    def test_conversation_is_labelled_in_order(self):
        prompt, last = protocol.build_prompt([
            {'role': 'system', 'content': 'be brief'}, {'role': 'user', 'content': 'one'},
            {'role': 'assistant', 'content': 'two'}, {'role': 'user', 'content': [{'type': 'text', 'text': 'three'}]}])
        self.assertEqual(last, 'three')
        self.assertLess(prompt.index('System:\nbe brief'), prompt.index('User:\none'))
        self.assertLess(prompt.index('Assistant:\ntwo'), prompt.index('User:\nthree'))

    def test_last_message_must_be_from_the_user(self):
        with self.assertRaises(protocol.RequestError):
            protocol.build_prompt([{'role': 'assistant', 'content': 'x'}])


class EventStream(unittest.TestCase):
    """Streamed API answers, which the fictional CLIs above do not produce."""

    def execution(self, sink):
        from omni_ai.server.execution import Execution
        execution = Execution('openrouter', 'p', 'p')
        execution.on_delta = sink
        execution._line('{"type":"smart.provider","provider":"openrouter"}')
        return execution

    def test_deltas_are_forwarded_and_the_final_answer_kept(self):
        pieces = []
        execution = self.execution(pieces.append)
        execution._line('{"type":"smart.delta","text":"he"}')
        execution._line('{"type":"smart.delta","text":"llo"}')
        execution._line('{"type":"smart.answer","content":"hello","model":"m","usage":{"prompt_tokens":1,"completion_tokens":2}}')
        self.assertEqual(pieces, ['he', 'llo'])
        self.assertEqual((execution.answer, execution.tokens), ('hello', (1, 2)))

    def test_masked_data_waits_for_the_restored_answer(self):
        pieces = []
        execution = self.execution(pieces.append)
        execution._line('  🛡️  PII mascarado: 1 item(s)')
        execution._line('{"type":"smart.delta","text":"[EMAIL"}')
        execution._line('{"type":"smart.answer","content":"a@b.com"}')
        self.assertEqual(pieces, [])
        self.assertFalse(execution.sent_delta)
        self.assertEqual(execution.answer, 'a@b.com')

    def test_provider_change_after_text_stops_the_request(self):
        from omni_ai.server.execution import SWITCHED
        execution = self.execution(lambda text: None)
        execution._line('{"type":"smart.delta","text":"partial"}')
        execution._line('{"type":"smart.provider","provider":"codex"}')
        self.assertEqual(execution.stop_reason, SWITCHED)

    def test_provider_change_before_text_discards_the_failed_attempt(self):
        execution = self.execution(lambda text: None)
        execution._line('{"type":"result","is_error":true,"result":"usage limit reached"}')
        self.assertTrue(execution.protocol_error and execution.limited)
        execution._line('{"type":"smart.provider","provider":"codex"}')
        self.assertFalse(execution.protocol_error)
        self.assertEqual(execution.provider, 'codex')

    def test_antigravity_result_is_the_answer(self):
        execution = self.execution(None)
        execution._line('{"event":"step_update","step_update":{"text_delta":"OK"}}')
        execution._line('{"event":"result","result":{"status":"SUCCESS","response":"OK\\n","usage":{"input_tokens":5,"output_tokens":2}}}')
        self.assertEqual((execution.answer, execution.tokens), ('OK', (5, 2)))
        self.assertFalse(execution.protocol_error)

    def test_antigravity_failure_is_an_error(self):
        execution = self.execution(None)
        execution._line('{"event":"result","result":{"status":"ERROR","error":"quota exceeded"}}')
        self.assertTrue(execution.protocol_error and execution.limited)

    def test_client_gone_while_streaming_stops_the_request(self):
        from omni_ai.server.execution import DISCONNECTED

        def broken(text):
            raise BrokenPipeError()

        execution = self.execution(broken)
        execution._line('{"type":"smart.delta","text":"x"}')
        self.assertEqual(execution.stop_reason, DISCONNECTED)


class ExternalKey(unittest.TestCase):
    def setUp(self):
        from omni_ai.server import auth
        self.auth = auth
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / 'secret'

    def test_key_file_is_used_and_trimmed(self):
        self.file.write_text('from-file\n')
        with patch.dict(os.environ, {'SMART_AI_SERVER_KEY_FILE': str(self.file), 'SMART_AI_SERVER_KEY': 'ignored'}):
            self.assertEqual(self.auth.external_key(), 'from-file')

    def test_unreadable_or_empty_file_fails_instead_of_generating_a_key(self):
        self.file.write_text('  ')
        for path in (self.file, Path(self.tmp.name) / 'missing'):
            with patch.dict(os.environ, {'SMART_AI_SERVER_KEY_FILE': str(path)}), self.assertRaises(SystemExit):
                self.auth.configured()

    def test_tools_do_not_inherit_the_key_settings(self):
        from omni_ai.server.execution import Execution
        with patch.dict(os.environ, {'SMART_AI_SERVER_KEY': 'k', 'SMART_AI_SERVER_KEY_FILE': '/run/secrets/k'}):
            env = Execution('codex', 'p', 'p')._env()
        self.assertNotIn('SMART_AI_SERVER_KEY', env)
        self.assertNotIn('SMART_AI_SERVER_KEY_FILE', env)


class WriteMode(unittest.TestCase):
    @staticmethod
    def _execution(**options):
        from omni_ai.server.execution import Execution
        return Execution('codex', 'p', 'p', **options)

    def test_plan_stays_read_only_by_default(self):
        env = self._execution()._env()
        self.assertEqual((env['SMART_AI_READ_ONLY'], env['SMART_AI_MODE']), ('1', 'plan'))

    def test_chosen_mode_reaches_the_tools(self):
        env = self._execution(mode='yolo', workdir='/w')._env()
        self.assertEqual((env['SMART_AI_READ_ONLY'], env['SMART_AI_MODE']), ('0', 'yolo'))

    def test_shell_mode_is_not_inherited(self):
        with patch.dict(os.environ, {'SMART_AI_MODE': 'yolo'}):
            self.assertEqual(self._execution()._env()['SMART_AI_MODE'], 'plan')

    def _main(self, *args):
        from omni_ai import server
        with patch('sys.stderr'), self.assertRaises(SystemExit) as caught:
            server.main(list(args))
        return caught.exception.code

    def test_writing_needs_a_folder(self):
        self.assertEqual(self._main('--mode', 'auto'), 2)

    def test_folder_must_exist(self):
        self.assertEqual(self._main('--mode', 'auto', '--workdir', '/does/not/exist'), 2)

    def test_unknown_mode_is_refused(self):
        self.assertEqual(self._main('--mode', 'default'), 2)

    def test_requests_run_in_the_folder_in_parallel(self):
        from omni_ai import server
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'Server', side_effect=OSError('stop')) as built, \
                patch.object(server.auth, 'configured', return_value=True), patch('sys.stderr'):
            self.assertEqual(server.main(['--mode', 'edits', '--workdir', folder]), 1)
        self.assertEqual(built.call_args.args[1:], (4, 600, 'edits', os.path.abspath(folder)))

    def test_plan_keeps_parallel_requests(self):
        from omni_ai import server
        with patch.object(server, 'Server', side_effect=OSError('stop')) as built, \
                patch.object(server, 'uses_shared_credentials', return_value=False), \
                patch.object(server.auth, 'configured', return_value=True), patch('sys.stderr'):
            self.assertEqual(server.main([]), 1)
        self.assertEqual(built.call_args.args[1:], (4, 600, 'plan', None))


class CursorReadOnly(unittest.TestCase):
    def test_plan_uses_the_mode_that_does_not_write(self):
        from omni_ai.cli import executors
        with patch.dict(os.environ, {'SMART_AI_READ_ONLY': '1'}), patch.object(executors, 'installed', return_value=True), \
                patch.object(executors, 'run_with_key_retry', return_value=0) as called, patch.object(executors, 'header'):
            executors.run_cursor('p')
        argv = called.call_args.args[1]
        self.assertEqual(argv[argv.index('--mode') + 1], 'ask')


class SharedCredentials(unittest.TestCase):
    def check(self, conf):
        from omni_ai.server import execution
        with patch.object(execution.config_tool, 'profiles', return_value={'profiles': {'p': conf}}), \
                patch.object(execution.config_tool, 'enabled_profiles', return_value=['p']):
            return execution.uses_shared_credentials()

    def test_only_a_profile_that_really_swaps_files_counts(self):
        self.assertFalse(self.check({'credentials_file': '~/.claude/.credentials.json'}))
        self.assertFalse(self.check({'config_dir': '/x'}))
        self.assertTrue(self.check({'credentials_file': '~/other.json'}))


class ApiClientParameters(unittest.TestCase):
    def test_grok_client_sends_request_parameters(self):
        from omni_ai import grok
        sent = {}

        def fake(endpoint, key, data=None, **_):
            sent.update(data)
            return {'choices': [{'message': {'content': 'ok'}}]}

        with patch.object(grok, 'request', fake), patch.dict(os.environ, {'SMART_AI_TEMPERATURE': '0.0', 'SMART_AI_MAX_TOKENS': '9'}):
            grok.chat('k', 'hi', 'model', mask=False)
        self.assertEqual((sent['temperature'], sent['max_tokens']), (0.0, 9))


if __name__ == '__main__':
    unittest.main()
