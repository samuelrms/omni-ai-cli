import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    'default': ('', '', ''),
    'plan': ('--permission-mode plan', '--sandbox read-only', '--mode plan'),
    'edits': ('--permission-mode acceptEdits', '--sandbox workspace-write', '--mode accept-edits'),
    'auto': ('--permission-mode auto', 'approval_policy="never"', '--mode auto'),
    'yolo': ('--permission-mode bypassPermissions', '--dangerously-bypass-approvals-and-sandbox', '--dangerously-skip-permissions'),
}


class ModeTests(unittest.TestCase):
    def test_each_mode_maps_to_every_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = dict(os.environ, SMART_AI_STATE=str(root / 'state'), SMART_AI_LOG=str(root / 'log'),
                       SMART_AI_HOME=str(root / 'cfg'))
            for tool in ('claude', 'codex', 'agy'):
                fake = root / tool
                fake.write_text(f'#!/bin/sh\necho "CALL {tool} $*"\n')
                fake.chmod(0o700)
                env[tool.upper() + '_BIN'] = str(fake)
            for mode, flags in EXPECTED.items():
                for tool, flag in zip(('claude', 'codex', 'agy'), flags):
                    out = subprocess.run([sys.executable, str(ROOT / 'smart_ai.py'), '--mode', mode, '--' + tool, 'x'],
                                         env=env, capture_output=True, text=True)
                    call = next((l for l in out.stdout.splitlines() if l.startswith('CALL ' + tool)), None)
                    self.assertIsNotNone(call, f'{mode}/{tool}: {out.stdout}{out.stderr}')
                    self.assertIn(flag, call, f'{mode}/{tool}')
                    if mode == 'default':
                        self.assertNotIn('--permission-mode', call)
                        self.assertNotIn('--sandbox', call)

    def test_invalid_mode_rejected(self):
        out = subprocess.run([sys.executable, str(ROOT / 'smart_ai.py'), '--mode', 'nope', 'x'], capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)


if __name__ == '__main__':
    unittest.main()
