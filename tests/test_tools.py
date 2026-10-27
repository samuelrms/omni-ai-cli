import os
from pathlib import Path
import sys
import unittest

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


if __name__ == '__main__':
    unittest.main()
