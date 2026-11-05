import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import cli  # noqa: E402


class UpdateTests(unittest.TestCase):
    """`ai --update` pulls the current branch from origin, so it works without upstream tracking."""

    def run_update(self, branch):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        (Path(temp.name) / '.git').mkdir()
        calls = []

        class Done:
            stdout = branch + '\n'

        with patch.object(cli.commands, 'REPO', Path(temp.name)), \
                patch.object(cli.commands, 'resolve_bin', return_value='git'), \
                patch.object(cli.subprocess, 'run', return_value=Done()), \
                patch.object(cli.subprocess, 'call', side_effect=lambda cmd: calls.append(cmd) or 0), \
                patch.object(cli, 'say'):
            cli.update()
        return calls[0]

    def test_pulls_current_branch_from_origin(self):
        cmd = self.run_update('feat/doctor')
        self.assertEqual(cmd[-3:], ['--ff-only', 'origin', 'feat/doctor'])

    def test_detached_head_falls_back_to_main(self):
        cmd = self.run_update('HEAD')
        self.assertEqual(cmd[-2:], ['origin', 'main'])


if __name__ == '__main__':
    unittest.main()
