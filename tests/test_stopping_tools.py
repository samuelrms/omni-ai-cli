"""Stopping a request stops the tool, also when it ignores the polite signal or outlives its parent."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from omni_ai import osutils
from omni_ai.chat.runner import Runner
from omni_ai.server import execution

ROOT = Path(__file__).resolve().parent.parent
# The parent dies on the first signal; the tool under it ignores that signal and keeps the output open
STUBBORN = f"""#!/bin/sh
"{sys.executable}" -c 'import os, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open(sys.argv[1], "w").write(str(os.getpid())); print("ready", flush=True); time.sleep(60)' "$1" &
wait
"""


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@unittest.skipIf(os.name == 'nt', 'POSIX signals')
class Stubborn(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.script = self.root / 'tool.sh'
        self.script.write_text(STUBBORN)
        self.script.chmod(0o700)
        self.pid_file = self.root / 'pid'
        self.addCleanup(self.kill_leftover)

    def kill_leftover(self):
        try:
            os.kill(int(self.pid_file.read_text()), signal.SIGKILL)
        except (OSError, ValueError):
            pass

    def end_group(self, process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.wait()

    def tool_pid(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            text = self.pid_file.read_text() if self.pid_file.exists() else ''
            if text.strip():
                return int(text)
            time.sleep(0.02)
        self.fail('the tool did not start')

    def assert_gone(self, pid, seconds=4):
        deadline = time.time() + seconds
        while time.time() < deadline and alive(pid):
            time.sleep(0.05)
        self.assertFalse(alive(pid), 'the tool is still running')

    def test_the_tree_goes_even_when_the_parent_dies_first(self):
        process = subprocess.Popen([str(self.script), str(self.pid_file)], stdout=subprocess.DEVNULL, **osutils.group_kwargs())
        self.addCleanup(self.end_group, process)
        pid = self.tool_pid()
        osutils.stop_tree(process, 0.3)
        process.wait(3)
        self.assertTrue(alive(pid), 'the polite signal should not have been enough here')
        self.assert_gone(pid)

    def test_a_server_request_ends_and_takes_the_tool_with_it(self):
        with patch.object(execution, 'FORCE_AFTER', 0.3), \
                patch.object(execution, 'module_cmd', return_value=[str(self.script), str(self.pid_file)]):
            run = execution.Execution('codex', 'p', 'p', workdir=str(self.root))
            result = []
            thread = threading.Thread(target=lambda: result.append(run.run()), daemon=True)
            thread.start()
            pid = self.tool_pid()
            run.stop(execution.TIMEOUT)
            thread.join(6)
        self.assertEqual(result, [execution.TIMEOUT])
        self.assert_gone(pid)

    def test_stop_after_the_parent_is_already_dead_still_reaches_the_tool(self):
        with patch.object(execution, 'FORCE_AFTER', 0.3), \
                patch.object(execution, 'module_cmd', return_value=[str(self.script), str(self.pid_file)]):
            run = execution.Execution('codex', 'p', 'p', workdir=str(self.root))
            thread = threading.Thread(target=run.run, daemon=True)
            thread.start()
            pid = self.tool_pid()
            os.kill(run.child.pid, signal.SIGKILL)
            run.child.wait(3)
            run.stop(execution.TIMEOUT)
            thread.join(6)
        self.assert_gone(pid)

    def test_chat_cancel_escalates_too(self):
        process = subprocess.Popen([str(self.script), str(self.pid_file)], stdout=subprocess.DEVNULL, **osutils.group_kwargs())
        self.addCleanup(self.end_group, process)
        pid = self.tool_pid()
        runner = Runner.__new__(Runner)
        runner.cancelled, runner.lock, runner.children = threading.Event(), threading.Lock(), {process}
        with patch('omni_ai.chat.runner.FORCE_AFTER', 0.3):
            runner.cancel()
        self.assertTrue(runner.cancelled.is_set())
        self.assert_gone(pid)


@unittest.skipIf(os.name == 'nt', 'POSIX pipes')
class ReaderGoesAway(unittest.TestCase):
    """`ai --codex x | head -1`"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pid_file = self.root / 'pid'
        codex = self.root / 'codex'
        codex.write_text(f'#!/bin/sh\necho $$ > "{self.pid_file}"\nwhile :; do echo line; sleep 0.05; done\n')
        codex.chmod(0o700)
        self.env = {**os.environ, 'HOME': str(self.root), 'PYTHONPATH': str(ROOT), 'CODEX_BIN': str(codex),
                    'SMART_AI_HOME': str(self.root / 'cfg')}

    def test_the_tool_stops_and_the_exit_code_says_why(self):
        process = subprocess.Popen([sys.executable, '-c', 'from omni_ai.cli import entry; import sys; sys.exit(entry())', '--codex', 'x'],
                                   env=self.env, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        process.stdout.readline()
        deadline = time.time() + 5
        while not self.pid_file.exists() and time.time() < deadline:
            time.sleep(0.02)
        pid = int(self.pid_file.read_text())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        process.stdout.close()
        self.assertEqual(process.wait(10), 141)
        deadline = time.time() + 5
        while time.time() < deadline and alive(pid):
            time.sleep(0.05)
        self.assertFalse(alive(pid), 'the tool is still running')


if __name__ == '__main__':
    unittest.main()
