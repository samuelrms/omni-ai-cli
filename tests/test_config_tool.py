import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)


class ConfigToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.profiles = root / 'profiles.json'
        self.profiles.write_text(json.dumps({'profiles': {
            'primary': {'priority': 1, 'enabled': True},
            'backup': {'priority': 2, 'enabled': False},
            'extra': {'priority': 3, 'enabled': True}}}))
        fake = root / 'fake-claude'
        fake.write_text('#!/bin/sh\nexit 0\n')
        fake.chmod(0o700)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('SMART_AI_')}
        main = root / 'home' / '.claude'
        (main / 'skills' / 'demo').mkdir(parents=True)
        (main / 'CLAUDE.md').write_text('rules')
        (root / 'home' / '.claude.json').write_text(json.dumps({'mcpServers': {'obsidian': {'command': 'x'}}, 'oauthAccount': 'secret'}))
        self.env.update(SMART_AI_CLAUDE_HOME=str(main), SMART_AI_HOME=str(root / 'cfg'), SMART_AI_CONFIG=str(self.profiles),
                        SMART_AI_ACCOUNTS=str(root / 'accounts'), CLAUDE_BIN=str(fake))

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *args, **extra):
        return subprocess.run([sys.executable, '-m', 'omni_ai.config', *args],
                              env=dict(self.env, **extra), capture_output=True, text=True)

    def test_model_set_validate_reset_and_env_priority(self):
        self.assertEqual(self.run_tool('model', 'codex', 'gpt-x').returncode, 0)
        self.assertIn("SMART_AI_CODEX_MODEL=gpt-x", self.run_tool('env').stdout)
        self.assertNotEqual(self.run_tool('model', 'codex_effort', 'ultra').returncode, 0)
        self.assertNotEqual(self.run_tool('model', 'openrouter', 'paid/model').returncode, 0)
        self.assertEqual(self.run_tool('model', 'openrouter_paid', 'on').returncode, 0)
        self.assertEqual(self.run_tool('model', 'openrouter', 'paid/model').returncode, 0)
        self.run_tool('model', 'openrouter_paid', 'off')
        self.assertIn("SMART_AI_OPENROUTER_MODEL=''", self.run_tool('env').stdout)
        self.assertNotEqual(self.run_tool('model', 'codex', 'x; rm -rf /').returncode, 0)
        self.assertIn('SMART_AI_CODEX_MODEL=env-model', self.run_tool('env', SMART_AI_CODEX_MODEL='env-model').stdout)
        self.run_tool('model', 'codex', 'default')
        self.assertIn('SMART_AI_CODEX_MODEL=gpt-6-astra', self.run_tool('env').stdout)

    def test_profiles_are_enabled_and_ordered(self):
        self.assertEqual(self.run_tool('profiles').stdout.split(), ['primary', 'extra'])

    def test_account_add_uses_isolated_dir_and_remove(self):
        self.assertEqual(self.run_tool('account', 'add', 'pessoal').returncode, 0)
        data = json.loads(self.profiles.read_text())['profiles']['pessoal']
        self.assertEqual(data['priority'], 4)
        account = Path(data['config_dir'])
        self.assertTrue((account / 'skills' / 'demo').is_dir())
        self.assertEqual((account / 'CLAUDE.md').read_text(), 'rules')
        state = json.loads((account / '.claude.json').read_text())
        self.assertIn('obsidian', state['mcpServers'])
        self.assertNotIn('oauthAccount', state)
        self.assertNotEqual(self.run_tool('account', 'remove', 'primary').returncode, 0)
        self.assertEqual(self.run_tool('account', 'remove', 'pessoal').returncode, 0)
        self.assertNotIn('pessoal', json.loads(self.profiles.read_text())['profiles'])
        self.assertNotEqual(self.run_tool('account', 'add', '../x').returncode, 0)

    def test_keys_hidden_storage_prefix_check_and_remove(self):
        bad = subprocess.run([sys.executable, '-m', 'omni_ai.config', 'key', 'claude'], env=self.env,
                             input='sk-wrong-prefix-1234567890\n', capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        good = subprocess.run([sys.executable, '-m', 'omni_ai.config', 'key', 'set', 'claude'], env=self.env,
                              input='sk-ant-abcdefghijklmnop1234\n', capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        path = Path(self.env['SMART_AI_HOME']) / 'keys' / 'claude'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_tool('key', 'get', 'claude').stdout.strip(), 'sk-ant-abcdefghijklmnop1234')
        listing = self.run_tool('key').stdout
        self.assertNotIn('abcdefghijklmnop', listing)
        self.assertIn('env-key', self.run_tool('key', 'get', 'codex', CODEX_API_KEY='env-key').stdout)
        self.run_tool('key', 'remove', 'claude')
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()


class MultipleKeysTests(unittest.TestCase):
    def test_add_list_and_remove_keys_in_order(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from omni_ai import config as cfg
        with tempfile.TemporaryDirectory() as d, patch.object(cfg, 'KEYS_DIR', Path(d) / 'keys'), \
                patch.object(cfg, 'LEGACY_OPENROUTER', Path(d) / 'legacy'), patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('OPENROUTER_API_KEY', None)
            cfg.cmd_key(['add', 'openrouter', 'sk-or-aaaaaaaaaaaaaaaa1'])  # first one becomes the main key
            cfg.cmd_key(['add', 'openrouter', 'sk-or-bbbbbbbbbbbbbbbb2'])
            cfg.cmd_key(['add', 'openrouter', 'trabalho', 'sk-or-cccccccccccccccc3'])
            names = [n for n, _, _ in cfg.stored_keys('openrouter')]
            self.assertEqual(names, ['principal', 'chave2', 'trabalho'])
            self.assertEqual(cfg.stored_key('openrouter')[0], 'sk-or-aaaaaaaaaaaaaaaa1')
            cfg.cmd_key(['remove', 'openrouter', 'chave2'])
            self.assertEqual([n for n, _, _ in cfg.stored_keys('openrouter')], ['principal', 'trabalho'])
            with self.assertRaises(SystemExit):
                cfg.cmd_key(['add', 'openrouter', 'trabalho', 'sk-or-dddddddddddddddd4'])
