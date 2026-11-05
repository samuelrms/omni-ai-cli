import isolation  # noqa: F401  (first: keeps the tests away from real data)
import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest

TESTS = Path(__file__).resolve().parent


class Isolation(unittest.TestCase):
    def _child(self, code):
        env = dict(os.environ, PYTHONPATH=f'{TESTS}{os.pathsep}{TESTS.parent}')
        return subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True)

    def test_every_test_module_imports_it_first(self):
        for path in TESTS.glob('test_*.py'):
            body = ast.parse(path.read_text(encoding='utf-8')).body
            imports = [n for n in body if isinstance(n, (ast.Import, ast.ImportFrom)) and getattr(n, 'module', '') != '__future__']
            self.assertEqual(imports[0].names[0].name, 'isolation', path.name)

    def test_home_is_not_the_real_one(self):
        self.assertNotEqual(Path.home().resolve(), isolation.REAL_HOME)

    def test_product_paths_stay_outside_the_real_home(self):
        out = self._child('import isolation\nfrom omni_ai import config\nfrom omni_ai.cli import base\n'
                          'print(config.KEYS_DIR, config.CONFIG_DIR, config.ACCOUNTS_DIR, base.STATE_FILE, base.LOG_FILE)')
        self.assertEqual(out.returncode, 0, out.stderr)
        for raw in out.stdout.split():
            self.assertNotIn(isolation.REAL_HOME, Path(raw).resolve().parents, raw)

    def test_real_tools_are_out_of_reach(self):
        out = self._child('import isolation, shutil\nprint([t for t in ("claude", "codex", "cursor-agent", "grok", "agy") if shutil.which(t)])')
        self.assertEqual(out.stdout.strip(), '[]')

    def test_secrets_are_removed(self):
        env = dict(os.environ, OPENROUTER_API_KEY='real', SMART_AI_HOME='/real', CODEX_BIN='/real', PYTHONPATH=str(TESTS))
        env.pop('OMNI_TEST_ISOLATED_PID')
        out = subprocess.run([sys.executable, '-c', 'import isolation, os\nprint([os.environ.get(k) for k in '
                              '("OPENROUTER_API_KEY", "SMART_AI_HOME", "CODEX_BIN")])'], env=env, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), '[None, None, None]')

    def test_writing_to_the_real_home_is_refused_and_fails_the_run(self):
        target = isolation.REAL_HOME / '.omni-test-must-not-exist'
        out = self._child(f'import isolation\ntry:\n    open({str(target)!r}, "w")\nexcept PermissionError:\n    print("refused")')
        self.assertIn('refused', out.stdout)
        self.assertEqual(out.returncode, 1)
        self.assertIn('real home folder', out.stderr)
        self.assertFalse(target.exists())

    def test_reading_the_real_home_is_allowed(self):
        out = self._child(f'import isolation, os\nprint(os.path.isdir({str(isolation.REAL_HOME)!r}))')
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == '__main__':
    unittest.main()
