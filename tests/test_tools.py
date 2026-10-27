import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import chat, cli, config, onboarding, ui  # noqa: E402
from omni_ai.tools import CLASSIFIER_KEYS, CLIS, PARALLEL, TOOLS  # noqa: E402


class RegistryTests(unittest.TestCase):
    """A tool added to tools.TOOLS shows up everywhere without touching the other modules."""

    def test_every_tool_has_a_runner(self):
        self.assertEqual(set(cli.runners()), set(TOOLS))

    def test_every_tool_is_listed_everywhere(self):
        for name, tool in TOOLS.items():
            with self.subTest(tool=name):
                self.assertIn(name, chat.ENGINES)
                self.assertIn(name, config.MODELS)
                self.assertIn(name, ui.TOOL_RGB)
                self.assertIn(name, cli.FLAGS)
                self.assertEqual(cli.FLAGS[tool.flag], name)
                self.assertIn('--' + name, cli.USAGE)
                self.assertIn(name, chat.tr('help'))
                if tool.key:
                    self.assertIn(name, config.KEYS)
                    self.assertIn(name, [provider for provider, _, _ in onboarding.KEYS])
                if tool.bin:
                    self.assertIn(name, cli.BINS)
                    self.assertIn(name, [agent[0] for agent in onboarding.AGENTS])

    def test_short_flags_are_unique_and_do_not_clash(self):
        flags = [tool.flag for tool in TOOLS.values()]
        self.assertEqual(len(flags), len(set(flags)))
        # single letters the router already uses for other options
        self.assertFalse(set(flags) & {'m', 's', 'r', 'l', 'h', 'i'})

    def test_classifier_keys_are_keys_but_not_tools(self):
        for name in CLASSIFIER_KEYS:
            self.assertIn(name, config.KEYS)
            self.assertNotIn(name, chat.ENGINES)

    def test_parallel_and_clis_are_real_tools(self):
        self.assertTrue(set(PARALLEL) <= set(TOOLS))
        self.assertTrue(set(CLIS) <= set(TOOLS))
        self.assertNotIn('claude', PARALLEL)  # /parallel subtasks are read-only and cheap

    def test_model_env_and_bin_override(self):
        self.assertEqual(config.MODELS['deepseek'][1], 'SMART_AI_DEEPSEEK_MODEL')
        self.assertEqual(list(config.MODELS)[:3], ['claude', 'codex', 'codex_effort'])
        self.assertIn('openrouter_paid', config.MODELS)
        if 'CURSOR_BIN' not in os.environ:
            self.assertEqual(cli.BINS['cursor'], 'cursor-agent')

    def test_model_label_falls_back_per_tool(self):
        self.assertEqual(chat.model_label('unknown', 'p'), 'p')
        self.assertTrue(chat.model_label('codex').endswith(config.model_values()['codex_effort']))



class InstallTests(unittest.TestCase):
    """First launch offers to install the missing tools with the official installers."""

    def setUp(self):
        self.present = {'claude', 'codex'}
        self.calls = []

        def resolve(name):
            return f'/bin/{name}' if name in self.present else None

        def call(argv):
            self.calls.append(argv[-1])
            tool = next(t for t in TOOLS.values() if t.install == argv[-1])
            self.present.add(tool.bin)  # the installer worked
            return 0
        for target in (patch.object(onboarding, 'resolve_bin', side_effect=resolve),
                       patch.object(onboarding.subprocess, 'call', side_effect=call),
                       patch.object(onboarding.os, 'name', 'posix'),
                       patch('sys.stdout', new_callable=io.StringIO)):
            self.out = target.start()
            self.addCleanup(target.stop)
        self.present.add('npm')

    def test_missing_lists_only_programs_not_installed(self):
        self.assertEqual(onboarding.missing(), [n for n in CLIS if TOOLS[n].bin not in self.present])
        self.assertNotIn('openrouter', onboarding.missing())  # API only

    def test_yes_runs_each_official_installer(self):
        with patch('builtins.input', return_value=''):
            installed = onboarding.install('pt')
        runnable = [n for n in CLIS if n not in ('claude', 'codex') and TOOLS[n].install_command()]
        self.assertEqual(installed, runnable)
        self.assertEqual(self.calls, [TOOLS[n].install for n in runnable])
        self.assertNotIn('agy', installed)  # download page, done by hand
        self.assertIn('antigravity.google', self.out.getvalue())

    def test_no_installs_nothing(self):
        with patch('builtins.input', return_value='n'):
            self.assertEqual(onboarding.install('pt'), [])
        self.assertEqual(self.calls, [])

    def test_npm_installers_need_npm(self):
        self.present -= {'codex', 'npm'}
        with patch('builtins.input', return_value='s'):
            installed = onboarding.install('pt', names=['codex'])
        self.assertEqual(installed, [])
        self.assertEqual(self.calls, [])
        self.assertIn('npm', self.out.getvalue())

    def test_failed_install_is_reported(self):
        with patch.object(onboarding.subprocess, 'call', return_value=1), patch('builtins.input', return_value=''):
            self.assertEqual(onboarding.install('pt', names=['grok']), [])
        self.assertIn('não foi instalado', self.out.getvalue())

    def test_nothing_missing(self):
        self.assertEqual(onboarding.install('pt', names=[]), [])
        self.assertIn('já estão instaladas', self.out.getvalue())

    def test_windows_uses_powershell_installers_only(self):
        self.assertIsNone(TOOLS['cursor'].install_command(windows=True))  # curl | bash only
        self.assertTrue(TOOLS['grok'].install_command(windows=True).startswith('irm '))

if __name__ == '__main__':
    unittest.main()
