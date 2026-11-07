import isolation  # noqa: F401  (first: keeps the tests away from real data)
import ast
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from omni_ai import cli, osutils
from omni_ai.cli import base, executors
from omni_ai.server import execution

ROOT = Path(__file__).resolve().parent.parent
# Over every limit: Windows 32,767 characters, Linux 128 KiB for one argument, macOS 1 MiB for everything
HUGE = 'á' * 1_100_000
RECORDER = '''#!{python}
import json, sys
stdin = sys.stdin.buffer.read().decode('utf-8')
with open({record!r}, 'w', encoding='utf-8') as out:
    json.dump({{'args': sys.argv[1:], 'stdin': stdin}}, out)
'''


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(os.path.realpath(tmp.name))


class PipedOutput(Temp):
    """A pipe on Windows starts in the ANSI code page, which has no ✓: the first status line crashed."""

    def run_piped(self, command):
        env = dict(os.environ, PYTHONIOENCODING='cp1252')
        return subprocess.run(command, env=osutils.module_env(env), cwd=self.folder, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=60)

    def test_the_ai_command_writes_utf8(self):
        done = self.run_piped([sys.executable, '-m', 'omni_ai', '--reset'])
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', 'replace'))
        self.assertIn('✓', done.stdout.decode('utf-8'))

    def test_the_router_started_by_the_chat_and_server_writes_utf8(self):
        done = self.run_piped(osutils.module_cmd('cli') + ['--reset'])
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', 'replace'))
        self.assertIn('✓', done.stdout.decode('utf-8'))

    def test_a_replaced_stream_is_left_alone(self):
        with patch.object(sys, 'stdout', io.StringIO()):
            osutils.utf8_stdio()

    def test_every_text_pipe_is_read_as_utf8(self):
        # Without an encoding Python uses the code page, and a byte it lacks stopped the reading thread
        missing = []
        for path in (ROOT / 'omni_ai').rglob('*.py'):
            if 'vendor' in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if not isinstance(node, ast.Call):
                    continue
                keywords = {k.arg: k.value for k in node.keywords if k.arg}
                textual = any(isinstance(keywords.get(name), ast.Constant) and keywords[name].value is True
                              for name in ('text', 'universal_newlines'))
                if textual and 'encoding' not in keywords:
                    missing.append(f'{path.relative_to(ROOT)}:{node.lineno}')
        self.assertEqual(missing, [])


@unittest.skipIf(os.name == 'nt', 'POSIX script; tests/test_windows_console.py covers Windows')
class LongPrompts(Temp):
    def setUp(self):
        super().setUp()
        self.record = self.folder / 'record.json'
        self.tool = self.folder / 'tool'
        self.tool.write_text(RECORDER.format(python=sys.executable, record=str(self.record)), encoding='utf-8')
        self.tool.chmod(0o755)

    def recorded(self):
        return json.loads(self.record.read_text(encoding='utf-8'))

    def run_tool(self, args, prompt_at, long_prompt):
        with patch.object(base, 'say'), patch.object(sys, 'stdout', io.StringIO()):
            return base.run([str(self.tool), *args], prompt_at=prompt_at, long_prompt=long_prompt)

    def test_claude_gets_a_long_prompt_from_stdin(self):
        code, _ = self.run_tool(['-p', HUGE, '--verbose'], 2, executors._without_prompt)
        self.assertEqual(code, 0)
        self.assertEqual(self.recorded(), {'args': ['-p', '--verbose'], 'stdin': HUGE})

    def test_codex_gets_a_long_prompt_from_stdin(self):
        code, _ = self.run_tool(['exec', '--', HUGE], 3, executors._prompt_from_stdin)
        self.assertEqual(code, 0)
        self.assertEqual(self.recorded(), {'args': ['exec', '--', '-'], 'stdin': HUGE})

    def test_a_short_prompt_stays_on_the_command_line(self):
        self.run_tool(['-p', 'olá', '--verbose'], 2, executors._without_prompt)
        self.assertEqual(self.recorded(), {'args': ['-p', 'olá', '--verbose'], 'stdin': ''})

    def test_a_tool_without_another_way_is_not_started(self):
        with patch.object(base, 'say') as say, patch.object(sys, 'stdout', io.StringIO()):
            code, _ = base.run([str(self.tool), '-p', HUGE], prompt_at=2)
        self.assertEqual(code, 126)
        self.assertIn('longo demais', say.call_args.args[0])
        self.assertFalse(self.record.exists())

    def test_a_tool_that_cannot_start_is_an_ordinary_failure(self):
        error = OSError(7, 'Argument list too long')
        with patch.object(base, 'say') as say, patch.object(base.subprocess, 'Popen', side_effect=error):
            code, _ = base.run([str(self.tool), 'x'])
        self.assertEqual(code, 126)
        self.assertIn('Argument list too long', say.call_args.args[0])


class ToolsSayWhereThePromptIs(unittest.TestCase):
    def captured(self, function, *args):
        calls = []
        def fake_run(argv, env=None, stdin=None, prompt_at=None, long_prompt=None):
            calls.append((argv, prompt_at, long_prompt))
            return 0, ''
        with patch.object(executors, 'run', side_effect=fake_run), patch.object(executors, 'installed', return_value=True), \
                patch.object(executors, 'over_ceiling', return_value=False), patch.object(executors, 'say'), \
                patch.object(executors, 'header'), patch.object(executors, 'log_usage'), \
                patch.object(executors, 'remember_limit'):
            function(*args)
        return calls[0]

    def test_each_tool_points_at_its_prompt(self):
        for function, args, long_prompt in ((executors.run_claude, ('primary', 'pedido'), executors._without_prompt),
                                            (executors.run_codex, ('pedido',), executors._prompt_from_stdin),
                                            (executors.run_agy, ('pedido',), None),
                                            (executors.run_cursor, ('pedido',), None)):
            with self.subTest(tool=function.__name__):
                argv, at, given = self.captured(function, *args)
                self.assertEqual(argv[at], 'pedido')
                self.assertIs(given, long_prompt)


class PromptsBetweenOurProcesses(Temp):
    def test_the_router_reads_the_prompt_from_stdin(self):
        stdin = io.TextIOWrapper(io.BytesIO('olá ✓\nsegunda linha'.encode('utf-8')), encoding='ascii')
        with patch.object(sys, 'stdin', stdin), patch.object(cli, 'route', return_value=0) as route:
            self.assertEqual(cli.main(['--codex', '--prompt-file', '-']), 0)
        route.assert_called_once_with('olá ✓\nsegunda linha', 'codex')

    def test_the_server_sends_the_prompt_through_stdin(self):
        script = self.folder / 'router.py'
        record = self.folder / 'record.json'
        script.write_text(RECORDER.format(python=sys.executable, record=str(record)), encoding='utf-8')
        prompt = 'á' * 40_000 + '\nfim'
        with patch.object(execution, 'module_cmd', return_value=[sys.executable, str(script)]):
            execution.Execution('codex', prompt, prompt, workdir=str(self.folder)).run()
        self.assertEqual(json.loads(record.read_text(encoding='utf-8')),
                         {'args': ['--codex', '--prompt-file', '-'], 'stdin': prompt})


class Cancel(unittest.TestCase):
    def test_a_legacy_profile_restores_its_login_on_every_stop_signal(self):
        self.assertIn(signal.SIGTERM, executors.STOP_SIGNALS)
        if os.name == 'nt':
            self.assertIn(signal.SIGBREAK, executors.STOP_SIGNALS)

    def test_send_prompt_keeps_line_breaks_as_they_are(self):
        # A text pipe on Windows writes every \n as \r\n; this one does the same anywhere
        class Kept(io.BytesIO):
            def close(self):
                self.kept = self.getvalue()
                super().close()
        raw = Kept()
        class Child:
            stdin = io.TextIOWrapper(raw, encoding='utf-8', newline='\r\n', write_through=True)
        osutils.send_prompt(Child, 'linha 1\nlinha 2 ✓')
        self.assertEqual(raw.kept, 'linha 1\nlinha 2 ✓'.encode('utf-8'))

    def test_send_prompt_survives_a_child_that_already_left(self):
        class Gone:
            class stdin:
                @staticmethod
                def write(text):
                    raise BrokenPipeError
        osutils.send_prompt(Gone, 'x')


if __name__ == '__main__':
    unittest.main()
