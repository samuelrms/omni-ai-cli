"""Runs on real Windows in CI (the Windows shims job): stopping console tools, piped output, long prompts, ai.cmd."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from omni_ai import osutils
from omni_ai.cli import base, executors

ROOT = Path(__file__).resolve().parent.parent
NPM_SHIM = """@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\\node.exe" (
  SET "_prog=%dp0%\\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\node_modules\\tool\\cli.js" %*
"""
RECORDER_JS = """const fs = require('fs');
const chunks = [];
process.stdin.on('data', (c) => chunks.push(c));
process.stdin.on('end', () => fs.writeFileSync(process.env.RECORD,
  JSON.stringify({ args: process.argv.slice(2), stdin: Buffer.concat(chunks).toString('utf8') })));
"""
# A console tool with a child of its own, as Claude Code or Codex under the router
TREE = """import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
open(sys.argv[1], 'w').write(str(child.pid))
time.sleep(120)
"""


def alive(pid):
    out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True, text=True,
                         encoding='utf-8', errors='replace').stdout
    return str(pid) in out


@unittest.skipUnless(os.name == 'nt', 'real Windows only')
class WindowsConsole(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(os.path.realpath(tmp.name))

    def test_cancel_stops_a_console_tool_and_its_children(self):
        pid_file = self.folder / 'pid'
        script = self.folder / 'tree.py'
        script.write_text(TREE, encoding='utf-8')
        process = subprocess.Popen([sys.executable, str(script), str(pid_file)], **osutils.group_kwargs())
        self.addCleanup(lambda: subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True))
        deadline = time.time() + 10
        while not (pid_file.exists() and pid_file.read_text().strip()) and time.time() < deadline:
            time.sleep(0.05)
        grandchild = int(pid_file.read_text())
        osutils.kill_tree(process)  # the polite stop, before stop_tree's forced one
        process.wait(5)
        deadline = time.time() + 5
        while alive(grandchild) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(grandchild), 'the child of the tool is still running')

    def test_piped_output_is_utf8(self):
        env = osutils.module_env({k: v for k, v in os.environ.items() if k not in ('PYTHONIOENCODING', 'PYTHONUTF8')})
        done = subprocess.run([sys.executable, '-m', 'omni_ai', '--reset'], env=env, cwd=self.folder,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', 'replace'))
        self.assertIn('✓', done.stdout.decode('utf-8'))

    @unittest.skipUnless(shutil.which('node'), 'needs Node')
    def test_a_long_prompt_reaches_an_npm_tool_through_stdin(self):
        (self.folder / 'node_modules/tool').mkdir(parents=True)
        (self.folder / 'node_modules/tool/cli.js').write_text(RECORDER_JS, encoding='utf-8')
        shim = self.folder / 'tool.cmd'
        shim.write_text(NPM_SHIM, encoding='utf-8')
        record = self.folder / 'record.json'
        prompt = 'á "x" & echo planted\n' * 3000  # about 66 thousand characters, twice what Windows allows
        with patch.dict(os.environ, RECORD=str(record)), patch.object(base, 'say'), patch.object(sys, 'stdout', io.StringIO()):
            code, _ = base.run([str(shim), '-p', prompt, '--verbose'], prompt_at=2, long_prompt=executors._without_prompt)
        self.assertEqual(code, 0)
        got = json.loads(record.read_text(encoding='utf-8'))
        self.assertEqual(got['args'], ['-p', '--verbose'])
        # Not assertEqual: a diff of 66 thousand characters exhausts difflib's recursion
        self.assertTrue(got['stdin'] == prompt, f"stdin differs: {len(got['stdin'])} vs {len(prompt)} characters, "
                                                 f"starts {got['stdin'][:30]!r}")
        self.assertFalse((self.folder / 'planted').exists())

    def test_ai_cmd_runs_a_failed_request_only_once(self):
        # It used to run the request again with `python` whenever `py` returned an error
        done = subprocess.run(['cmd', '/d', '/c', str(ROOT / 'scripts' / 'ai.cmd'), '--mode', 'bad'], cwd=self.folder,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        output = (done.stdout + done.stderr).decode('utf-8', 'replace')
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(output.count('Modo inv'), 1, output)


if __name__ == '__main__':
    unittest.main()
