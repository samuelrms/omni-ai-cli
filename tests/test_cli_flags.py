import isolation  # noqa: F401  (first: keeps the tests away from real data)
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import cli  # noqa: E402


class CliFlagTests(unittest.TestCase):
    def output(self, argv):
        # main dispatches to functions across cli's submodules, each with its own `say`; patch all.
        lines = []
        say = lambda *a: lines.append(a[0] if a else '')
        mods = (cli, cli.base, cli.commands, cli.health, cli.usage_view, cli.executors, cli.routing)
        patches = [patch.object(m, 'say', say) for m in mods]
        for p in patches:
            p.start()
        try:
            code = cli.main(argv)
        finally:
            for p in patches:
                p.stop()
        return code, '\n'.join(lines)

    def test_version(self):
        with patch.object(cli, 'ai_version', return_value='9.9.9'):
            code, text = self.output(['--version'])
        self.assertEqual(code, 0)
        self.assertIn('9.9.9', text)

    def test_completions_zsh_and_bash(self):
        code, zsh = self.output(['--completions', 'zsh'])
        self.assertEqual(code, 0)
        self.assertIn('compdef _ai ai', zsh)
        self.assertIn('--doctor', zsh)
        code, bash = self.output(['--completions', 'bash'])
        self.assertEqual(code, 0)
        self.assertIn('complete -F _ai_complete ai', bash)

    def test_completions_bad_shell_fails(self):
        code, _ = self.output(['--completions', 'fish'])
        self.assertEqual(code, 1)

    def test_completion_words_cover_core_flags(self):
        words = cli.completion_words()
        for flag in ('--doctor', '--version', '--usage', '--claude', 'doctor'):
            self.assertIn(flag, words)


if __name__ == '__main__':
    unittest.main()
