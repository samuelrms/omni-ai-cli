"""Branch coverage for omni_ai.config: model settings, accounts, keys, profiles.

Everything runs in-process with subprocess/network stubbed and all filesystem
writes aimed at a temp dir, so the suite is deterministic and leaves no trace.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import config as cfg  # noqa: E402


class FakeRun:
    """Stand-in for a subprocess.run / subprocess.call result."""

    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class ConfigBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config_dir = self.root / 'cfg'
        self.config_dir.mkdir()
        self.profiles_file = self.root / 'profiles.json'
        self.write_profiles({'profiles': {
            'primary': {'priority': 1, 'enabled': True, 'name': 'Conta principal'},
            'backup': {'priority': 2, 'enabled': False, 'name': 'Reserva'},
            'extra': {'priority': 3, 'enabled': True, 'name': ''}}})
        # Claude main setup used by share_claude_setup
        self.main_claude = self.root / 'home' / '.claude'
        (self.main_claude / 'skills' / 'demo').mkdir(parents=True)
        (self.main_claude / 'CLAUDE.md').write_text('rules')
        (self.root / 'home' / '.claude.json').write_text(
            json.dumps({'mcpServers': {'obsidian': {'command': 'x'}}, 'oauthAccount': 'secret'}))

        consts = {
            'CONFIG_DIR': self.config_dir,
            'MODELS_FILE': self.config_dir / 'models.json',
            'KEYS_DIR': self.config_dir / 'keys',
            'LEGACY_OPENROUTER': self.config_dir / 'legacy_openrouter',
            'ACCOUNTS_DIR': self.root / 'accounts',
            'TOOL_ACCOUNTS_DIR': self.config_dir / 'accounts',
        }
        for name, value in consts.items():
            p = patch.object(cfg, name, value)
            p.start()
            self.addCleanup(p.stop)

        envp = patch.dict(os.environ, {}, clear=False)
        envp.start()
        self.addCleanup(envp.stop)
        for key in list(os.environ):
            if key.startswith('SMART_AI_'):
                os.environ.pop(key)
        for env, _, _ in cfg.KEYS.values():
            os.environ.pop(env, None)
        for _, (_, env, _) in cfg.MODELS.items():
            os.environ.pop(env, None)
        os.environ.pop('CLAUDE_BIN', None)
        os.environ['SMART_AI_CONFIG'] = str(self.profiles_file)
        os.environ['SMART_AI_CLAUDE_HOME'] = str(self.main_claude)

    def write_profiles(self, data):
        self.profiles_file.write_text(json.dumps(data))

    def read_profiles(self):
        return json.loads(self.profiles_file.read_text())

    def cap(self, fn, *args, **kwargs):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn(*args, **kwargs)
        return buf.getvalue()


class JsonHelperTests(ConfigBase):
    def test_read_write_roundtrip(self):
        target = self.config_dir / 'deep' / 'data.json'
        cfg.write_json(target, {'a': 1, 'ç': 'ã'})
        self.assertEqual(cfg.read_json(target, None), {'a': 1, 'ç': 'ã'})

    def test_read_json_default_on_missing(self):
        self.assertEqual(cfg.read_json(self.root / 'nope.json', {'d': 1}), {'d': 1})

    def test_read_json_corrupt_raises_not_overwrites(self):
        # A corrupt file must NOT read as the default (which would let the next write erase it).
        bad = self.config_dir / 'bad.json'
        bad.write_text('{not json')
        with self.assertRaises(SystemExit):
            cfg.read_json(bad, 'fallback')


class ProfilesPathTests(ConfigBase):
    def test_env_override_wins(self):
        self.assertEqual(cfg.profiles_path(), self.profiles_file)

    def test_template_is_copied_when_user_file_absent(self):
        os.environ.pop('SMART_AI_CONFIG')
        template = self.root / 'template.json'
        template.write_text(json.dumps({'profiles': {}}))
        with patch.object(cfg, 'TEMPLATE_PROFILES', template):
            path = cfg.profiles_path()
        self.assertEqual(path, self.config_dir / 'profiles.json')
        self.assertTrue(path.exists())

    def test_profiles_raises_when_unreadable(self):
        os.environ['SMART_AI_CONFIG'] = str(self.root / 'missing.json')
        with self.assertRaises(SystemExit):
            cfg.profiles()

    def test_profiles_adds_default_section(self):
        self.write_profiles({})
        self.assertEqual(cfg.profiles()['profiles'], {})


class ModelTests(ConfigBase):
    def test_model_values_env_then_saved_then_default(self):
        cfg.write_json(cfg.MODELS_FILE, {'codex': 'saved-model'})
        self.assertEqual(cfg.model_values()['codex'], 'saved-model')
        os.environ['SMART_AI_CODEX_MODEL'] = 'env-model'
        self.assertEqual(cfg.model_values()['codex'], 'env-model')
        self.assertEqual(cfg.model_values()['codex_effort'], 'high')

    def test_validate_model_rejects_unknown_key(self):
        with self.assertRaises(SystemExit):
            cfg.validate_model('nope', 'x')

    def test_validate_model_rejects_bad_chars(self):
        with self.assertRaises(SystemExit):
            cfg.validate_model('codex', 'bad value')

    def test_validate_model_effort_and_paid_switch(self):
        with self.assertRaises(SystemExit):
            cfg.validate_model('codex_effort', 'ultra')
        cfg.validate_model('codex_effort', 'high')
        with self.assertRaises(SystemExit):
            cfg.validate_model('openrouter_paid', 'maybe')
        cfg.validate_model('openrouter_paid', 'on')

    def test_validate_model_paid_openrouter_requires_authorisation(self):
        with self.assertRaises(SystemExit):
            cfg.validate_model('openrouter', 'vendor/paid-model')
        cfg.validate_model('openrouter', 'openrouter/free')
        cfg.validate_model('openrouter', 'vendor/model:free')
        cfg.write_json(cfg.MODELS_FILE, {'openrouter_paid': 'on'})
        cfg.validate_model('openrouter', 'vendor/paid-model')

    def test_cmd_model_list_and_single_key(self):
        out = self.cap(cfg.cmd_model, [])
        self.assertIn('codex', out)
        self.assertIn('padrão', out)
        single = self.cap(cfg.cmd_model, ['codex'])
        self.assertIn('codex =', single)
        self.cap(cfg.cmd_model, ['codex_effort'])

    def test_cmd_model_set_and_reset_default(self):
        out = self.cap(cfg.cmd_model, ['codex', 'gpt-custom'])
        self.assertIn('gpt-custom', out)
        self.assertEqual(cfg.read_json(cfg.MODELS_FILE, {})['codex'], 'gpt-custom')
        self.cap(cfg.cmd_model, ['codex', 'default'])
        self.assertNotIn('codex', cfg.read_json(cfg.MODELS_FILE, {}))

    def test_cmd_model_env_override_warning(self):
        os.environ['SMART_AI_CODEX_MODEL'] = 'env-model'
        out = self.cap(cfg.cmd_model, ['codex', 'gpt-custom'])
        self.assertIn('tem prioridade', out)

    def test_cmd_model_openrouter_paid_on_and_off(self):
        on = self.cap(cfg.cmd_model, ['openrouter_paid', 'on'])
        self.assertIn('Atenção', on)
        cfg.write_json(cfg.MODELS_FILE, {'openrouter': 'vendor/paid', 'openrouter_paid': 'on'})
        off = self.cap(cfg.cmd_model, ['openrouter_paid', 'off'])
        self.assertIn('removido', off)
        self.assertNotIn('openrouter', cfg.read_json(cfg.MODELS_FILE, {}))

    def test_cmd_env_emits_shell_assignments(self):
        out = self.cap(cfg.cmd_env)
        self.assertIn('SMART_AI_CODEX_MODEL=', out)


class ProfileListingTests(ConfigBase):
    def test_ordered_by_priority(self):
        names = [name for name, _ in cfg.ordered(cfg.profiles())]
        self.assertEqual(names, ['primary', 'backup', 'extra'])

    def test_enabled_profiles_and_cmd(self):
        self.assertEqual(cfg.enabled_profiles(), ['primary', 'extra'])
        self.assertEqual(self.cap(cfg.cmd_profiles).split(), ['primary', 'extra'])


class LoggedInTests(ConfigBase):
    def test_false_when_config_dir_missing(self):
        self.assertFalse(cfg.logged_in({'config_dir': str(self.root / 'ghost')}))

    def test_false_when_credentials_file_missing(self):
        self.assertFalse(cfg.logged_in({'credentials_file': str(self.root / 'ghost.json')}))

    def test_true_from_json_status(self):
        with patch.object(cfg.subprocess, 'run', return_value=FakeRun(0, '{"loggedIn": true, "email": "a@b"}')):
            self.assertTrue(cfg.logged_in({}))

    def test_authmethod_none_is_not_logged_in(self):
        with patch.object(cfg.subprocess, 'run', return_value=FakeRun(0, '{"authMethod": "none"}')):
            self.assertFalse(cfg.logged_in({}))

    def test_none_when_cli_errors(self):
        with patch.object(cfg.subprocess, 'run', side_effect=OSError):
            self.assertIsNone(cfg.logged_in({}))

    def test_fallback_when_output_is_not_json(self):
        with patch.object(cfg.subprocess, 'run', return_value=FakeRun(0, 'all good')):
            self.assertTrue(cfg.logged_in({}))
        with patch.object(cfg.subprocess, 'run', return_value=FakeRun(1, 'not logged in')):
            self.assertFalse(cfg.logged_in({}))


class ShareSetupTests(ConfigBase):
    def test_links_shared_items_and_copies_mcp(self):
        account = self.root / 'accounts' / 'pessoal'
        account.mkdir(parents=True)
        cfg.share_claude_setup(account)
        self.assertTrue((account / 'skills' / 'demo').is_dir())
        self.assertEqual((account / 'CLAUDE.md').read_text(), 'rules')
        state = json.loads((account / '.claude.json').read_text())
        self.assertIn('obsidian', state['mcpServers'])
        self.assertNotIn('oauthAccount', state)

    def test_login_runs_cli_and_shares(self):
        account = self.root / 'accounts' / 'nova'
        with patch.object(cfg.subprocess, 'call', return_value=0) as call:
            code = self.cap(cfg.login, 'nova', {'config_dir': str(account)})
        self.assertEqual(call.call_count, 1)
        self.assertIn('Skills', code)


class AccountCommandTests(ConfigBase):
    def test_list(self):
        with patch.object(cfg, 'logged_in', return_value=True):
            out = self.cap(cfg.cmd_account, ['list'])
        self.assertIn('primary', out)
        self.assertIn('backup', out)

    def test_add_writes_profile(self):
        with patch.object(cfg, 'login', return_value=0), patch.object(cfg, 'logged_in', return_value=False):
            out = self.cap(cfg.cmd_account, ['add', 'pessoal'])
        self.assertIn('adicionada', out)
        saved = self.read_profiles()['profiles']['pessoal']
        self.assertEqual(saved['priority'], 4)
        self.assertEqual(saved['config_dir'], str(self.root / 'accounts' / 'pessoal'))

    def test_add_aborts_when_login_fails(self):
        with patch.object(cfg, 'login', return_value=1), patch.object(cfg, 'logged_in', return_value=False):
            with self.assertRaises(SystemExit):
                cfg.cmd_account(['add', 'pessoal'])

    def test_add_existing_logged_in_rejected(self):
        self.write_profiles({'profiles': {'pessoal': {'config_dir': str(self.root / 'x'), 'priority': 1}}})
        with patch.object(cfg, 'logged_in', return_value=True):
            with self.assertRaises(SystemExit):
                cfg.cmd_account(['add', 'pessoal'])

    def test_bad_name_and_missing_name(self):
        with self.assertRaises(SystemExit):
            cfg.cmd_account(['add'])
        with self.assertRaises(SystemExit):
            cfg.cmd_account(['add', '../escape'])

    def test_login_existing_and_primary_shortcut(self):
        self.write_profiles({'profiles': {
            'primary': {'priority': 1, 'enabled': True},
            'nova': {'priority': 2, 'config_dir': str(self.root / 'accounts' / 'nova')}}})
        out = self.cap(cfg.cmd_account, ['login', 'primary'])
        self.assertIn('claude auth login', out)
        with patch.object(cfg, 'login', return_value=0):
            out = self.cap(cfg.cmd_account, ['login', 'nova'])
        self.assertIn('atualizado', out)

    def test_login_unknown_account(self):
        with self.assertRaises(SystemExit):
            cfg.cmd_account(['login', 'ghost'])

    def test_sync_variants(self):
        self.write_profiles({'profiles': {
            'primary': {'priority': 1},
            'nova': {'priority': 2, 'config_dir': str(self.root / 'accounts' / 'nova')}}})
        out = self.cap(cfg.cmd_account, ['sync', 'primary'])
        self.assertIn('nada a sincronizar', out)
        with patch.object(cfg, 'share_claude_setup') as share:
            out = self.cap(cfg.cmd_account, ['sync', 'nova'])
        share.assert_called_once()
        self.assertIn('sincronizada', out)

    def test_enable_disable(self):
        self.cap(cfg.cmd_account, ['disable', 'extra'])
        self.assertFalse(self.read_profiles()['profiles']['extra']['enabled'])
        self.cap(cfg.cmd_account, ['enable', 'extra'])
        self.assertTrue(self.read_profiles()['profiles']['extra']['enabled'])

    def test_remove(self):
        folder = self.root / 'accounts' / 'nova'
        folder.mkdir(parents=True)
        self.write_profiles({'profiles': {
            'primary': {'priority': 1},
            'nova': {'priority': 2, 'config_dir': str(folder)}}})
        with patch.object(cfg.subprocess, 'run', return_value=FakeRun(0)):
            out = self.cap(cfg.cmd_account, ['remove', 'nova'])
        self.assertIn('removida', out)
        self.assertNotIn('nova', self.read_profiles()['profiles'])
        self.assertFalse(folder.exists())

    def test_remove_primary_blocked(self):
        with self.assertRaises(SystemExit):
            cfg.cmd_account(['remove', 'primary'])

    def test_invalid_action(self):
        with self.assertRaises(SystemExit):
            cfg.cmd_account(['bogus', 'primary'])

    def test_sync_all(self):
        folder = self.root / 'accounts' / 'nova'
        folder.mkdir(parents=True)
        self.write_profiles({'profiles': {'nova': {'priority': 1, 'config_dir': str(folder)}}})
        with patch.object(cfg, 'share_claude_setup') as share:
            out = self.cap(cfg.cmd_account, ['sync-all'])
        share.assert_called_once()
        self.assertIn('atualizada', out)

    def test_claude_alias_routes_to_list(self):
        with patch.object(cfg, 'logged_in', return_value=True):
            out = self.cap(cfg.cmd_account, ['list', 'claude'])
        self.assertIn('primary', out)


class ToolAccountTests(ConfigBase):
    def test_tool_env_expands_home(self):
        env = cfg.tool_env('codex', {'config_dir': str(self.root / 'c')})
        self.assertEqual(env, {'CODEX_HOME': str(self.root / 'c')})

    def test_tool_accounts_ordering_and_enabled_filter(self):
        self.write_profiles({'profiles': {}, 'accounts': {'codex': {
            'a': {'priority': 2, 'enabled': True},
            'b': {'priority': 1, 'enabled': False}}}})
        self.assertEqual([n for n, _ in cfg.tool_accounts('codex')], ['b', 'a'])
        self.assertEqual([n for n, _ in cfg.tool_accounts('codex', enabled_only=True)], ['a'])

    def test_tool_logged_in(self):
        self.assertFalse(cfg.tool_logged_in('codex', {'config_dir': str(self.root / 'ghost')}))
        folder = self.root / 'codexhome'
        folder.mkdir()
        with patch.object(cfg.subprocess, 'run', return_value=FakeRun(0)):
            self.assertTrue(cfg.tool_logged_in('codex', {'config_dir': str(folder)}))
        grok = self.root / 'grokhome'
        grok.mkdir()
        self.assertFalse(cfg.tool_logged_in('grok', {'config_dir': str(grok)}))
        (grok / 'auth.json').write_text('{}')
        self.assertTrue(cfg.tool_logged_in('grok', {'config_dir': str(grok)}))

    def test_share_tool_setup_links(self):
        main = self.root / 'codexmain'
        (main / 'skills').mkdir(parents=True)
        (main / 'config.toml').write_text('x')
        homes = dict(cfg.TOOL_HOMES, codex=('CODEX_HOME', str(main), ('config.toml', 'skills'), ['codex', 'login']))
        target = self.root / 'codexacct'
        with patch.object(cfg, 'TOOL_HOMES', homes):
            cfg.share_tool_setup('codex', target)
        self.assertTrue((target / 'config.toml').exists())
        self.assertTrue((target / 'skills').exists())

    def test_cmd_tool_account_list_add_login_toggle_remove(self):
        self.write_profiles({'profiles': {}})
        out = self.cap(cfg.cmd_tool_account, 'codex', 'list', [])
        self.assertIn('codex', out)
        with patch.object(cfg, 'tool_login', return_value=0), \
                patch.object(cfg, 'tool_logged_in', return_value=True):
            out = self.cap(cfg.cmd_tool_account, 'codex', 'add', ['trabalho'])
        self.assertIn('adicionada', out)
        self.assertIn('trabalho', self.read_profiles()['accounts']['codex'])
        with patch.object(cfg, 'tool_login', return_value=0):
            self.cap(cfg.cmd_tool_account, 'codex', 'login', ['trabalho'])
        self.cap(cfg.cmd_tool_account, 'codex', 'disable', ['trabalho'])
        self.assertFalse(self.read_profiles()['accounts']['codex']['trabalho']['enabled'])
        with patch.object(cfg.subprocess, 'run', return_value=FakeRun(0)):
            out = self.cap(cfg.cmd_tool_account, 'codex', 'remove', ['trabalho'])
        self.assertIn('removida', out)

    def test_cmd_tool_account_errors(self):
        self.write_profiles({'profiles': {}})
        with self.assertRaises(SystemExit):
            cfg.cmd_tool_account('codex', 'add', ['primary'])
        with self.assertRaises(SystemExit):
            cfg.cmd_tool_account('codex', 'login', ['ghost'])
        with self.assertRaises(SystemExit):
            cfg.cmd_tool_account('codex', 'bogus', ['x'])

    def test_cmd_account_routes_to_tool(self):
        self.write_profiles({'profiles': {}})
        with patch.object(cfg, 'tool_login', return_value=0), \
                patch.object(cfg, 'tool_logged_in', return_value=True):
            out = self.cap(cfg.cmd_account, ['add', 'codex', 'trabalho'])
        self.assertIn('adicionada', out)


class KeyTests(ConfigBase):
    def test_save_key_permissions(self):
        path = cfg.key_path('claude')
        cfg.save_key(path, 'sk-ant-abcdefghijklmnop1234')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_stored_keys_env_main_and_extras(self):
        cfg.save_key(cfg.key_path('openrouter'), 'sk-or-mainmainmainmain1')
        cfg.save_key(cfg.extra_key_path('openrouter', 'work'), 'sk-or-workworkworkwork2')
        names = [n for n, _, _ in cfg.stored_keys('openrouter')]
        self.assertEqual(names, ['principal', 'work'])
        os.environ['OPENROUTER_API_KEY'] = 'sk-or-envenvenvenvenv3'
        self.assertEqual(cfg.stored_keys('openrouter')[0][0], 'env')
        self.assertEqual(cfg.stored_key('openrouter')[0], 'sk-or-envenvenvenvenv3')

    def test_stored_key_empty_when_none(self):
        self.assertEqual(cfg.stored_key('grok'), ('', ''))

    def test_next_key_name(self):
        self.assertEqual(cfg.next_key_name('grok'), 'chave2')

    def test_ask_key_validation(self):
        with self.assertRaises(SystemExit):
            cfg.ask_key('claude', 'short')
        with self.assertRaises(SystemExit):
            cfg.ask_key('claude', 'xx-abcdefghijklmnop1234')  # wrong prefix
        self.assertEqual(cfg.ask_key('claude', 'sk-ant-abcdefghijklmnop1234'), 'sk-ant-abcdefghijklmnop1234')

    def test_ask_key_uses_getpass_when_not_inline(self):
        with patch.object(cfg.getpass, 'getpass', return_value='xai-abcdefghijklmnop1234'), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cfg.ask_key('grok', ''), 'xai-abcdefghijklmnop1234')
        with patch.object(cfg.getpass, 'getpass', side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                cfg.ask_key('grok', '')

    def test_cmd_key_list_set_get_add_remove(self):
        out = self.cap(cfg.cmd_key, ['list'])
        self.assertIn('claude', out)
        self.cap(cfg.cmd_key, ['set', 'claude', 'sk-ant-abcdefghijklmnop1234'])
        self.assertEqual(self.cap(cfg.cmd_key, ['get', 'claude']).strip(), 'sk-ant-abcdefghijklmnop1234')
        # shorthand: provider name is the action
        self.cap(cfg.cmd_key, ['grok', 'xai-abcdefghijklmnop1234'])
        self.assertEqual(cfg.stored_key('grok')[0], 'xai-abcdefghijklmnop1234')
        self.cap(cfg.cmd_key, ['add', 'grok', 'segunda', 'xai-secondsecondsecond2'])
        self.assertIn('segunda', [n for n, _, _ in cfg.stored_keys('grok')])
        self.cap(cfg.cmd_key, ['remove', 'grok', 'segunda'])
        self.assertNotIn('segunda', [n for n, _, _ in cfg.stored_keys('grok')])
        out = self.cap(cfg.cmd_key, ['remove', 'grok'])
        self.assertIn('removida', out)

    def test_cmd_key_set_env_warning(self):
        os.environ['XAI_API_KEY'] = 'xai-fromenvfromenvfrom1'
        out = self.cap(cfg.cmd_key, ['set', 'grok', 'xai-abcdefghijklmnop1234'])
        self.assertIn('tentada primeiro', out)

    def test_cmd_key_add_duplicate_rejected(self):
        self.cap(cfg.cmd_key, ['add', 'grok', 'xai-abcdefghijklmnop1234'])  # becomes principal
        self.cap(cfg.cmd_key, ['add', 'grok', 'dupe', 'xai-dupedupedupedupe22'])
        with self.assertRaises(SystemExit):
            cfg.cmd_key(['add', 'grok', 'dupe', 'xai-otherotherotherot33'])

    def test_cmd_key_invalid_tool(self):
        with self.assertRaises(SystemExit):
            cfg.cmd_key(['get', 'notatool'])

    def test_cmd_key_remove_nothing(self):
        out = self.cap(cfg.cmd_key, ['remove', 'grok', 'ghost'])
        self.assertIn('Nenhuma', out)


class MainDispatchTests(ConfigBase):
    def test_dispatch(self):
        self.assertIn('SMART_AI_CODEX_MODEL=', self.cap(cfg.main, ['env']))
        self.assertIn('codex', self.cap(cfg.main, ['model']))
        self.assertIn('codex', self.cap(cfg.main, ['key', 'list']))
        self.assertEqual(self.cap(cfg.main, ['profiles']).split(), ['primary', 'extra'])
        with patch.object(cfg, 'logged_in', return_value=True):
            self.assertIn('primary', self.cap(cfg.main, ['account', 'list']))
        self.assertIn('codex', self.cap(cfg.main, []))  # defaults to model
        with self.assertRaises(SystemExit):
            cfg.main(['bogus'])


if __name__ == '__main__':
    unittest.main()
