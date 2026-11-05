"""state.json writers must not lose each other's updates across threads and processes."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from omni_ai import cli  # noqa: E402

WORKER = """
import sys
from omni_ai.cli import base, limits, commands
worker = sys.argv[1]
for i in range(25):
    base.set_state(f'{worker}_{i}', i)
    limits.remember_limit(f'{worker}:{i}', 'You hit your usage limit, resets at 7pm')
"""


class StateLocking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / 'state.json'
        for module in (cli, cli.base, cli.limits, cli.commands):
            patcher = patch.object(module, 'STATE_FILE', self.state_file)
            patcher.start()
            self.addCleanup(patcher.stop)

    def stored(self):
        return json.loads(self.state_file.read_text(encoding='utf-8'))

    def test_threads_keep_every_update(self):
        def work(worker):
            for i in range(25):
                cli.base.set_state(f'{worker}_{i}', i)
                cli.limits.remember_limit(f'{worker}:{i}', 'You hit your usage limit, resets at 7pm')

        threads = [threading.Thread(target=work, args=(f'w{n}',)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        data = self.stored()
        self.assertEqual(sum(1 for key in data if key != 'limits'), 150)
        self.assertEqual(len(data['limits']), 150)

    @unittest.skipIf(os.name == 'nt', 'update_json has no cross-process lock on Windows')
    def test_processes_keep_every_update(self):
        env = dict(os.environ, SMART_AI_STATE=str(self.state_file), PYTHONPATH=str(ROOT))
        workers = [subprocess.Popen([sys.executable, '-c', WORKER, f'w{n}'], env=env, cwd=self.tmp.name)
                   for n in range(4)]
        for worker in workers:
            self.assertEqual(worker.wait(timeout=60), 0)
        data = self.stored()
        self.assertEqual(sum(1 for key in data if key != 'limits'), 100)
        self.assertEqual(len(data['limits']), 100)

    def test_expired_cooldown_is_cleared(self):
        cli.base.set_state('p1_last_rate_limit', '1000')
        self.assertEqual(cli.base.cooldown_left('p1'), 0)
        self.assertEqual(self.stored()['p1_last_rate_limit'], '')

    def test_expiring_cooldown_keeps_a_newer_rate_limit(self):
        # Another process records a new limit between the read and the clear.
        fresh = str(int(time.time()))
        cli.base.set_state('p1_last_rate_limit', fresh)
        with patch.object(cli.base, 'state', return_value={'p1_last_rate_limit': '1000'}):
            left = cli.base.cooldown_left('p1')
        self.assertGreater(left, 0)
        self.assertEqual(self.stored()['p1_last_rate_limit'], fresh)

    def test_reset_keeps_other_keys(self):
        cli.base.set_state('p1_last_rate_limit', '123')
        cli.base.set_state('other', 'kept')
        with patch.object(cli.commands, 'say'):
            cli.commands.reset_cooldowns()
        self.assertEqual(self.stored(), {'p1_last_rate_limit': '', 'other': 'kept'})


if __name__ == '__main__':
    unittest.main()
