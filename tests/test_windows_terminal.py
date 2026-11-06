import isolation  # noqa: F401
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SHELLS = {'powershell': ['powershell', '-NoProfile', '-Command'], 'pwsh': ['pwsh', '-NoProfile', '-Command'], 'cmd': ['cmd', '/d', '/s', '/c']}
TEXTS = ('x&(echo.planted)>proof.txt&y', 'a b"&(echo.planted)>proof.txt&"c d', '" & echo planted> proof.txt & "',
         "it's; echo planted> proof.txt; 'y", '%PATH% !x! ^& $(echo planted> proof.txt)', 'ação\nsecond line')


@unittest.skipUnless(os.name == 'nt', 'needs Windows shells')
class ExtensionTerminalLine(unittest.TestCase):
    """The same line the extension sends, in each shell, through the ai.cmd that install.ps1 creates."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(os.path.realpath(tmp.name))
        self.cwd = self.folder / 'workspace'
        self.cwd.mkdir()
        self.proof = self.cwd / 'proof.txt'
        package = self.folder / 'package'
        package.mkdir()
        (package / 'package.json').write_text(json.dumps({'name': 'omni-echo', 'version': '1.0.0', 'bin': {'omni-echo': 'cli.js'}}))
        (package / 'cli.js').write_text('#!/usr/bin/env node\nconsole.log(JSON.stringify(process.argv.slice(2)));\n')
        prefix = self.folder / 'prefix'
        subprocess.run([shutil.which('npm'), 'install', '-g', '--prefix', str(prefix), str(package)], check=True, capture_output=True)
        launcher = self.folder / 'ai.cmd'
        launcher.write_text(f'@echo off\nset "PYTHONPATH={ROOT}"\n"{sys.executable}" -P -m omni_ai %*\n')
        self.launcher = str(launcher)
        self.env = dict(os.environ, PATH=str(self.folder) + os.pathsep + os.environ['PATH'], PYTHONUTF8='1',
                        CODEX_BIN=str(prefix / 'omni-echo.cmd'))
        self.request = self.cwd / 'request.txt'

    def line_in(self, shell, line):
        return subprocess.run(SHELLS[shell] + [line], cwd=self.cwd, env=self.env, capture_output=True, text=True,
                              encoding='utf-8', errors='replace')

    def test_the_old_line_ran_text_as_a_command(self):
        self.request.write_text('x&(echo.planted)>proof.txt&y', encoding='utf-8')
        self.line_in('powershell', f"& '{self.launcher}' --codex \"$(cat '{self.request}')\"")
        self.assertTrue(self.proof.exists())

    def test_the_request_reaches_the_tool_as_written_and_runs_nothing(self):
        for shell in SHELLS:
            call = '& ' if shell != 'cmd' else ''
            quoted = f"'{self.launcher}'" if shell != 'cmd' else f'"{self.launcher}"'
            for text in TEXTS:
                with self.subTest(shell=shell, text=text):
                    self.request.write_text(text, encoding='utf-8')
                    out = self.line_in(shell, f'{call}{quoted} --codex --prompt-file "{self.request}"')
                    self.assertFalse(self.proof.exists(), out.stdout + out.stderr)
                    sent = [line for line in out.stdout.splitlines() if line.startswith('[')]
                    self.assertTrue(sent, out.stdout + out.stderr)
                    self.assertEqual(json.loads(sent[-1])[-1], text)


if __name__ == '__main__':
    unittest.main()
