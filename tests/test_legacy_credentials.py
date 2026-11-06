import isolation  # noqa: F401  (first: keeps the tests away from real data)
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

CLAUDE = """#!/bin/sh
cat "$HOME/.claude/.credentials.json" > "$HOME/seen" 2>/dev/null
touch "$HOME/ready"
case "$FAKE_SLEEP" in 1) sleep 30 ;; esac
echo done
"""
ROUTER = """
import sys
from omni_ai.cli import executors as e
e.installed = lambda name: True
e.profile_conf = lambda name: {'credentials_file': sys.argv[1]}
e.models = lambda: {'claude': 'm'}
sys.exit(e.run_claude('second', 'hi'))
"""


@unittest.skipIf(os.name == 'nt', 'POSIX signals')
class LegacySwap(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        (self.home / '.claude').mkdir()
        self.main = self.home / '.claude' / '.credentials.json'
        self.backup = self.home / '.claude' / '.credentials.omni-backup'
        self.other = self.home / 'second.json'
        self.other.write_text('SECOND')
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        claude = bin_dir / 'claude'
        claude.write_text(CLAUDE)
        claude.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PYTHONPATH=str(ROOT), PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')

    def start(self, sleep=True):
        env = dict(self.env, FAKE_SLEEP='1' if sleep else '0')
        return subprocess.Popen([sys.executable, '-P', '-c', ROUTER, str(self.other)], env=env, cwd=self.home,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def wait_ready(self, process):
        deadline = time.time() + 20
        while not (self.home / 'ready').exists():
            self.assertLess(time.time(), deadline, 'the fake CLI did not start')
            self.assertIsNone(process.poll(), 'the router ended early')
            time.sleep(0.02)
        self.assertEqual((self.home / 'seen').read_text(), 'SECOND')

    def finish(self):
        process = self.start(sleep=False)
        self.assertEqual(process.wait(timeout=20), 0)

    def test_normal_run_restores_and_cleans_up(self):
        self.main.write_text('MAIN')
        self.finish()
        self.assertEqual(self.main.read_text(), 'MAIN')
        self.assertFalse(self.backup.exists())

    def test_sigterm_restores_the_main_login(self):
        self.main.write_text('MAIN')
        process = self.start()
        self.wait_ready(process)
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=20)
        self.assertEqual(self.main.read_text(), 'MAIN')
        self.assertFalse(self.backup.exists())

    def test_sigkill_is_healed_on_the_next_run(self):
        self.main.write_text('MAIN')
        process = self.start()
        self.wait_ready(process)
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=20)
        self.assertEqual(self.main.read_text(), 'SECOND')  # nothing can run after SIGKILL
        self.assertEqual(self.backup.read_text(), 'MAIN')
        self.finish()
        self.assertEqual(self.main.read_text(), 'MAIN')
        self.assertFalse(self.backup.exists())

    def test_no_main_login_stays_absent_and_token_is_private(self):
        process = self.start()
        self.wait_ready(process)
        self.assertEqual(stat.S_IMODE(self.main.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backup.stat().st_mode), 0o600)
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=20)
        self.assertFalse(self.main.exists())
        self.assertFalse(self.backup.exists())


if __name__ == '__main__':
    unittest.main()
