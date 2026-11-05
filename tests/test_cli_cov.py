"""Coverage-focused tests for omni_ai/cli.py.

Every test is hermetic: no network, no real subprocess, no real filesystem writes outside a
per-test temp dir. The subprocess wrapper `cli.run`, `subprocess.run/call/Popen`, `input()`,
`urllib`, and the provider runners are all mocked.
"""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import cli  # noqa: E402


# A full model mapping so run_* never touches config_tool.model_values().
MODELS = {'claude': 'opus', 'codex': 'gpt', 'codex_effort': 'low', 'cursor': '', 'agy': '',
          'openrouter': '', 'openrouter_paid': 'off', 'grok': '', 'deepseek': ''}


# After cli.py became a package, a constant/function lives in the submodule that uses it (imported by
# value) rather than only on the package. Patch each name on every submodule that holds it, so a single
# setUp/capture call affects all callers regardless of which submodule the function under test lives in.
_SAY_MODS = (cli, cli.base, cli.executors, cli.routing, cli.health, cli.usage_view, cli.commands)
_LOG_MODS = (cli, cli.base, cli.usage_view, cli.commands, cli.health)
_STATE_MODS = (cli, cli.base, cli.limits, cli.commands)


def _patch_all(mods, name, value):
    return [patch.object(mod, name, value) for mod in mods]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state_file = root / 'state.json'
        self.log_file = root / 'usage.log'
        targets = (_patch_all(_STATE_MODS, 'STATE_FILE', self.state_file)
                   + _patch_all(_LOG_MODS, 'LOG_FILE', self.log_file)
                   + [patch.dict(os.environ, {'SMART_AI_MODE': 'default', 'SMART_AI_EVENTS': '0',
                                              'SMART_AI_READ_ONLY': '0', 'SMART_AI_CLASSIFY_PROMPT': ''},
                                 clear=False)])
        for target in targets:
            target.start()
            self.addCleanup(target.stop)

    def capture(self, func, *a, **k):
        lines = []
        patches = _patch_all(_SAY_MODS, 'say', lambda *x: lines.append(x[0] if x else ''))
        for p in patches:
            p.start()
        try:
            result = func(*a, **k)
        finally:
            for p in patches:
                p.stop()
        return result, '\n'.join(lines)

    def main(self, argv):
        return self.capture(cli.main, argv)


# ─── pure helpers ────────────────────────────────────────────────

class PureHelperTests(Base):
    def test_key_account(self):
        self.assertEqual(cli.key_account('codex', 'principal'), 'codex:api-key')
        self.assertEqual(cli.key_account('codex', 'extra'), 'codex:api-key:extra')
        self.assertEqual(cli.key_account('', 'env'), 'api-key')
        self.assertEqual(cli.key_account('', 'foo'), 'api-key:foo')

    def test_ago(self):
        self.assertEqual(cli.ago(90), '1m')
        self.assertEqual(cli.ago(3600), '1h00m')
        self.assertEqual(cli.ago(3 * 3600 + 5 * 60), '3h05m')

    def test_mode_from_env(self):
        with patch.dict(os.environ, {'SMART_AI_READ_ONLY': '1'}):
            self.assertEqual(cli.mode(), 'plan')
        with patch.dict(os.environ, {'SMART_AI_READ_ONLY': '0', 'SMART_AI_MODE': 'yolo'}):
            self.assertEqual(cli.mode(), 'yolo')

    def test_limit_until_relative_and_absolute_and_none(self):
        base = 1_000_000
        self.assertEqual(cli.limit_until('resets in 2h30m', base), base + 2 * 3600 + 30 * 60)
        self.assertIsNone(cli.limit_until('no time here', base))
        self.assertIsInstance(cli.limit_until('try again at 11pm', base), int)
        self.assertIsNone(cli.limit_until('resets at 99:99', base))

    def test_active_limit_expiry(self):
        self.assertIsNone(cli.active_limit(None))
        item = {'text': 'resets in 1h', 'at': 1000, 'until': 1000 + 3600}
        self.assertIsNotNone(cli.active_limit(item, now=2000))
        self.assertIsNone(cli.active_limit(item, now=1000 + 3600 + 1))

    def test_hit_limit_text_mode(self):
        self.assertTrue(cli.hit_limit('boom\nusage limit reached', code=1))
        self.assertFalse(cli.hit_limit('all good', code=0))

    def test_hit_limit_events_mode(self):
        with patch.dict(os.environ, {'SMART_AI_EVENTS': '1'}):
            evt = json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}})
            self.assertTrue(cli.hit_limit(evt, code=0))
            err = json.dumps({'type': 'result', 'is_error': True, 'result': 'quota reached'})
            self.assertTrue(cli.hit_limit(err, code=0))
            ok = json.dumps({'type': 'result', 'is_error': False, 'result': 'done'})
            self.assertFalse(cli.hit_limit(ok, code=0))

    def test_completion_words(self):
        words = cli.completion_words()
        for flag in ('--doctor', '--version', '--sync', '--menu-json' if False else '--usage', '--claude', 'doctor'):
            self.assertIn(flag, words)


# ─── state / log / cooldown ──────────────────────────────────────

class StateTests(Base):
    def test_set_and_read_state(self):
        cli.set_state('foo', 'bar')
        self.assertEqual(cli.state()['foo'], 'bar')

    def test_log_usage_writes(self):
        cli.log_usage('claude', 'p1', 'success')
        self.assertIn('tool=claude', self.log_file.read_text())

    def test_cooldown_left_zero_when_unset(self):
        self.assertEqual(cli.cooldown_left('p1'), 0)

    def test_cooldown_left_active_and_expired(self):
        import time
        cli.set_state('p1_last_rate_limit', str(int(time.time())))
        with patch.object(cli.base, 'profile_conf', return_value={'cooldown_minutes': 60}):
            self.assertGreater(cli.cooldown_left('p1'), 0)
        # far in the past → expired, cleared
        cli.set_state('p1_last_rate_limit', '1000')
        with patch.object(cli.base, 'profile_conf', return_value={'cooldown_minutes': 1}):
            self.assertEqual(cli.cooldown_left('p1'), 0)

    def test_mark_rate_limited(self):
        _, text = self.capture(cli.mark_rate_limited, 'p1')
        self.assertIn('rate limit', text)
        self.assertTrue(cli.state().get('p1_last_rate_limit'))

    def test_reset_cooldowns(self):
        cli.set_state('p1_last_rate_limit', '123')
        _, text = self.capture(cli.reset_cooldowns)
        self.assertEqual(cli.state()['p1_last_rate_limit'], '')
        self.assertIn('resetados', text)

    def test_remember_limit_store_and_clear(self):
        cli.remember_limit('claude:p1', 'You hit your usage limit, resets at 7pm')
        self.assertIn('claude:p1', cli.state()['limits'])
        cli.remember_limit('claude:p1', 'no limit words')
        self.assertNotIn('claude:p1', cli.state().get('limits', {}))

    def test_show_log_empty_and_with_lines(self):
        _, text = self.capture(cli.show_log)
        self.assertIn('Nenhum log', text)
        self.log_file.write_text('\n'.join(f'line{i}' for i in range(30)))
        _, text = self.capture(cli.show_log)
        self.assertIn('line29', text)
        self.assertNotIn('line9\n', text)  # only last 20


# ─── classification ──────────────────────────────────────────────

class ClassifyTests(Base):
    def test_classify_rules_complex_keywords(self):
        with patch.object(cli.classifier, 'config', return_value={'routing': {'complex_keywords': ['refatorar']}}):
            self.assertEqual(cli.classify_rules('refatorar isso'), 'complex')

    def test_classify_rules_workspace_regex(self):
        with patch.object(cli.classifier, 'config', return_value={'routing': {}}):
            self.assertEqual(cli.classify_rules('edite o repositório'), 'complex')
            self.assertEqual(cli.classify_rules('rode o script main.py'), 'complex')
            self.assertEqual(cli.classify_rules('veja ./src/app'), 'complex')

    def test_classify_rules_simple_short(self):
        with patch.object(cli.classifier, 'config', return_value={'routing': {'max_simple_length': 120}}):
            self.assertEqual(cli.classify_rules('o que significa REST'), 'simple')

    def test_classify_rules_simple_keyword(self):
        with patch.object(cli.classifier, 'config', return_value={'routing': {'simple_keywords': ['traduz']}}):
            self.assertEqual(cli.classify_rules('traduz ' + 'x' * 200), 'simple')

    def test_classify_rules_long_falls_complex(self):
        with patch.object(cli.classifier, 'config', return_value={'routing': {}}):
            self.assertEqual(cli.classify_rules('explique o conceito de ' + 'palavra ' * 40), 'complex')

    def test_classify_prefers_llm(self):
        with patch.object(cli.classifier, 'classify_llm', return_value='simple'):
            self.assertEqual(cli.classify('anything'), 'simple')

    def test_classify_falls_back_to_rules(self):
        with patch.object(cli.classifier, 'classify_llm', return_value=None), \
                patch.object(cli.classifier, 'classify_rules', return_value='complex') as rules:
            self.assertEqual(cli.classify('x'), 'complex')
            rules.assert_called_once()

    def test_classify_uses_env_override(self):
        with patch.dict(os.environ, {'SMART_AI_CLASSIFY_PROMPT': 'forced prompt'}), \
                patch.object(cli.classifier, 'classify_llm', return_value=None), \
                patch.object(cli.classifier, 'classify_rules', side_effect=lambda p: p) as rules:
            cli.classify('ignored')
            rules.assert_called_once_with('forced prompt')

    def test_ask_text_success_and_failure(self):
        payload = {'choices': [{'message': {'content': 'complex'}}]}
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__ = lambda s: resp
        resp.__exit__ = lambda s, *a: False
        with patch('urllib.request.urlopen', return_value=resp):
            self.assertEqual(cli.ask_text('http://x', 'k', {}), 'complex')
        with patch('urllib.request.urlopen', side_effect=OSError):
            self.assertIsNone(cli.ask_text('http://x', 'k', {}))

    def test_ask_classifier_finds_label(self):
        with patch.object(cli.classifier, 'ask_text', return_value='The answer is SIMPLE here'):
            self.assertEqual(cli.ask_classifier('u', 'k', {}), 'simple')
        with patch.object(cli.classifier, 'ask_text', return_value=None):
            self.assertIsNone(cli.ask_classifier('u', 'k', {}))

    def test_free_classifiers_structure(self):
        backends = cli.free_classifiers()
        providers = {p for p, _, _ in backends}
        self.assertTrue({'groq', 'cerebras', 'openrouter'} <= providers)

    def test_classify_llm_picks_first_answering_backend(self):
        def stored_keys(provider):
            return [('principal', 'key', 'salva')] if provider == 'groq' else []
        with patch.object(cli.config_tool, 'stored_keys', side_effect=stored_keys), \
                patch.object(cli.classifier, 'models', return_value=MODELS), \
                patch.object(cli.classifier, 'ask_classifier', return_value='complex'):
            self.assertEqual(cli.classify_llm('do something'), 'complex')
            self.assertEqual(cli.CLASSIFIED_BY[0], 'groq')

    def test_classify_llm_none_when_no_keys(self):
        with patch.object(cli.config_tool, 'stored_keys', return_value=[]), \
                patch.object(cli.classifier, 'models', return_value=MODELS):
            self.assertIsNone(cli.classify_llm('do something'))


# ─── run() subprocess wrapper ────────────────────────────────────

class RunWrapperTests(Base):
    def test_run_missing_binary(self):
        with patch.object(cli.base, 'resolve_bin', return_value=None):
            (code, out), _ = self.capture(cli.run, ['nope', 'arg'])
        self.assertEqual(code, 127)

    def test_run_streams_and_returns_output(self):
        fake = MagicMock()
        fake.stdout = iter(['hello\n', 'world\n'])
        fake.wait.return_value = 0
        with patch.object(cli.base, 'resolve_bin', return_value='/bin/tool'), \
                patch.object(cli.subprocess, 'Popen', return_value=fake), \
                patch.object(cli.sys.stdout, 'write'), patch.object(cli.sys.stdout, 'flush'):
            code, out = cli.run(['tool'])
        self.assertEqual(code, 0)
        self.assertEqual(out, 'hello\nworld\n')

    def test_run_writes_stdin(self):
        fake = MagicMock()
        fake.stdout = iter([])
        fake.wait.return_value = 0
        fake.stdin = MagicMock()
        with patch.object(cli.base, 'resolve_bin', return_value='/bin/tool'), \
                patch.object(cli.subprocess, 'Popen', return_value=fake), \
                patch.object(cli.sys.stdout, 'write'), patch.object(cli.sys.stdout, 'flush'):
            cli.run(['tool'], stdin='data')
        fake.stdin.write.assert_called_once_with('data')


# ─── run_claude ──────────────────────────────────────────────────

class RunClaudeTests(Base):
    def test_not_installed(self):
        with patch.object(cli.executors, 'installed', return_value=False):
            code, text = self.capture(cli.run_claude, 'p1', 'hi')
        self.assertEqual(code, 1)
        self.assertIn('não instalado', text)

    def test_api_key_success(self):
        with patch.object(cli.executors, 'installed', return_value=True), \
                patch.object(cli.executors, 'models', return_value=MODELS), \
                patch.object(cli.config_tool, 'stored_keys', return_value=[('principal', 'sk-ant-x', 'salva')]), \
                patch.object(cli.config_tool, 'stored_key', return_value=('sk-ant-x', 'salva')), \
                patch.object(cli.executors, 'run', return_value=(0, 'ok')):
            code, text = self.capture(cli.run_claude, 'api-key', 'hi')
        self.assertEqual(code, 0)

    def test_rate_limited_marks_cooldown(self):
        with patch.object(cli.executors, 'installed', return_value=True), \
                patch.object(cli.executors, 'models', return_value=MODELS), \
                patch.object(cli.config_tool, 'stored_keys', return_value=[('principal', 'sk-ant-x', 'salva')]), \
                patch.object(cli.config_tool, 'stored_key', return_value=('sk-ant-x', 'salva')), \
                patch.object(cli.executors, 'run', return_value=(1, 'usage limit reached')):
            code, text = self.capture(cli.run_claude, 'api-key', 'hi')
        self.assertEqual(code, cli.RATE_LIMITED)

    def test_config_dir_missing_is_not_ready(self):
        with patch.object(cli.executors, 'installed', return_value=True), \
                patch.object(cli.executors, 'models', return_value=MODELS), \
                patch.object(cli.executors, 'profile_conf', return_value={'config_dir': '/nonexistent/xyz'}):
            code, text = self.capture(cli.run_claude, 'p1', 'hi')
        self.assertEqual(code, cli.NOT_READY)
        self.assertIn('sem login', text)

    def test_config_dir_present_success(self):
        with patch.object(cli.executors, 'installed', return_value=True), \
                patch.object(cli.executors, 'models', return_value=MODELS), \
                patch.object(cli.executors, 'profile_conf', return_value={'config_dir': self.tmp.name}), \
                patch.object(cli.executors, 'run', return_value=(0, 'ok')):
            code, text = self.capture(cli.run_claude, 'p1', 'hi')
        self.assertEqual(code, 0)

    def test_credentials_file_missing_is_not_ready(self):
        missing = str(Path(self.tmp.name) / 'creds.json')  # never created
        with patch.object(cli.executors, 'installed', return_value=True), \
                patch.object(cli.executors, 'models', return_value=MODELS), \
                patch.object(cli.executors, 'profile_conf', return_value={'credentials_file': missing}):
            code, text = self.capture(cli.run_claude, 'p1', 'hi')
        self.assertEqual(code, cli.NOT_READY)


# ─── run_with_key_retry and provider runners ─────────────────────

class ProviderRunnerTests(Base):
    def _no_accounts(self):
        """Patches so run_with_key_retry sees only the primary login and no extra keys."""
        return [patch.object(cli.config_tool, 'tool_accounts', return_value=[]),
                patch.object(cli.config_tool, 'tool_env', return_value={}),
                patch.object(cli.config_tool, 'stored_keys', return_value=[]),
                patch.object(cli.executors, 'cooldown_left', return_value=0),
                patch.object(cli.executors, 'over_ceiling', return_value=False),
                patch.object(cli.executors, 'models', return_value=MODELS)]

    def test_run_with_key_retry_primary_success(self):
        for t in self._no_accounts():
            t.start()
            self.addCleanup(t.stop)
        with patch.object(cli.executors, 'run', return_value=(0, '')):
            code, _ = self.capture(cli.run_with_key_retry, 'codex', ['codex'], 'CODEX_API_KEY', 'Codex')
        self.assertEqual(code, 0)

    def test_run_with_key_retry_tries_extra_key(self):
        with patch.object(cli.config_tool, 'tool_accounts', return_value=[]), \
                patch.object(cli.config_tool, 'tool_env', return_value={}), \
                patch.object(cli.config_tool, 'stored_keys', return_value=[('k1', 'val', 'salva')]), \
                patch.object(cli.executors, 'cooldown_left', return_value=0), \
                patch.object(cli.executors, 'over_ceiling', return_value=False), \
                patch.object(cli.executors, 'models', return_value=MODELS), \
                patch.object(cli.executors, 'run', side_effect=[(1, 'limit reached'), (0, '')]) as run_mock:
            code, text = self.capture(cli.run_with_key_retry, 'codex', ['codex'], 'CODEX_API_KEY', 'Codex')
        self.assertEqual(code, 0)
        self.assertEqual(run_mock.call_count, 2)

    def test_run_codex(self):
        with patch.object(cli.executors, 'models', return_value=MODELS), \
                patch.object(cli.executors, 'run_with_key_retry', return_value=0) as rk:
            code, _ = self.capture(cli.run_codex, 'prompt')
        self.assertEqual(code, 0)
        rk.assert_called_once()

    def test_run_agy_with_events_and_model(self):
        models = dict(MODELS, agy='gemini-x')
        with patch.object(cli.executors, 'models', return_value=models), \
                patch.dict(os.environ, {'SMART_AI_EVENTS': '1'}), \
                patch.object(cli.executors, 'run_with_key_retry', return_value=0) as rk:
            code, _ = self.capture(cli.run_agy, 'prompt')
        self.assertEqual(code, 0)
        argv = rk.call_args[0][1]
        self.assertIn('--model', argv)
        self.assertIn('stream-json', argv)

    def test_run_cursor_not_installed(self):
        with patch.object(cli.executors, 'installed', return_value=False):
            code, text = self.capture(cli.run_cursor, 'prompt')
        self.assertEqual(code, 1)
        self.assertIn('não instalado', text)

    def test_run_cursor_installed(self):
        with patch.object(cli.executors, 'installed', return_value=True), \
                patch.object(cli.executors, 'models', return_value=MODELS), \
                patch.object(cli.executors, 'run_with_key_retry', return_value=0) as rk:
            code, _ = self.capture(cli.run_cursor, 'prompt')
        self.assertEqual(code, 0)
        rk.assert_called_once()

    def test_run_openrouter_no_key(self):
        with patch.object(cli.config_tool, 'stored_keys', return_value=[]):
            code, text = self.capture(cli.run_openrouter, 'prompt')
        self.assertEqual(code, 1)
        self.assertIn('OPENROUTER_API_KEY', text)

    def test_run_openrouter_success(self):
        with patch.object(cli.config_tool, 'stored_keys', return_value=[('principal', 'sk-or-x', 'salva')]), \
                patch.object(cli.executors, 'models', return_value=dict(MODELS, openrouter='model/x')), \
                patch.object(cli.executors, 'module_cmd', return_value=['py', '-m', 'x']), \
                patch.object(cli.executors, 'module_env', side_effect=lambda e: e), \
                patch.object(cli.executors, 'run', return_value=(0, '')):
            code, _ = self.capture(cli.run_openrouter, 'prompt')
        self.assertEqual(code, 0)

    def test_run_deepseek_no_key(self):
        with patch.object(cli.config_tool, 'stored_keys', return_value=[]):
            code, text = self.capture(cli.run_deepseek, 'prompt')
        self.assertEqual(code, 1)

    def test_run_deepseek_success_second_key(self):
        with patch.object(cli.config_tool, 'stored_keys',
                          return_value=[('k1', 'a', 'salva'), ('k2', 'b', 'salva')]), \
                patch.object(cli.executors, 'models', return_value=dict(MODELS, deepseek='deepseek-chat')), \
                patch.object(cli.executors, 'module_cmd', return_value=['py']), \
                patch.object(cli.executors, 'module_env', side_effect=lambda e: e), \
                patch.object(cli.executors, 'run', side_effect=[(1, ''), (0, '')]):
            code, text = self.capture(cli.run_deepseek, 'prompt')
        self.assertEqual(code, 0)
        self.assertIn('tentando com a chave k2', text)

    def test_run_grok_cli_path(self):
        with patch.object(cli.executors, 'installed', return_value=True), \
                patch.object(cli.executors, 'models', return_value=dict(MODELS, grok='grok-4')), \
                patch.object(cli.executors, 'run_with_key_retry', return_value=0) as rk:
            code, _ = self.capture(cli.run_grok, 'prompt')
        self.assertEqual(code, 0)
        rk.assert_called_once()

    def test_run_grok_api_no_key(self):
        with patch.object(cli.executors, 'installed', return_value=False), \
                patch.object(cli.executors, 'models', return_value=MODELS), \
                patch.object(cli.config_tool, 'stored_keys', return_value=[]):
            code, text = self.capture(cli.run_grok, 'prompt')
        self.assertEqual(code, 1)
        self.assertIn('não instalado', text)

    def test_run_grok_api_success(self):
        with patch.object(cli.executors, 'installed', return_value=False), \
                patch.object(cli.executors, 'models', return_value=dict(MODELS, grok='grok-4')), \
                patch.object(cli.config_tool, 'stored_keys', return_value=[('principal', 'xai-x', 'salva')]), \
                patch.object(cli.executors, 'module_cmd', return_value=['py']), \
                patch.object(cli.executors, 'module_env', side_effect=lambda e: e), \
                patch.object(cli.executors, 'run', return_value=(0, '')):
            code, _ = self.capture(cli.run_grok, 'prompt')
        self.assertEqual(code, 0)


# ─── over_ceiling ────────────────────────────────────────────────

class OverCeilingTests(Base):
    def test_warns_and_skips_above_ceiling(self):
        with patch.object(cli.usage, 'read', return_value={'windows': [{'used': 95}]}), \
                patch.object(cli.usage, 'alerts', return_value=['atenção 95%']), \
                patch.object(cli.usage, 'thresholds', return_value=(90, 80)), \
                patch.object(cli.usage, 'peak', return_value=95):
            skip, text = self.capture(cli.over_ceiling, 'claude:p1', lambda: None, 'Claude p1')
        self.assertTrue(skip)
        self.assertIn('pulando', text)

    def test_allows_below_ceiling(self):
        with patch.object(cli.usage, 'read', return_value={'windows': [{'used': 10}]}), \
                patch.object(cli.usage, 'alerts', return_value=[]), \
                patch.object(cli.usage, 'thresholds', return_value=(90, 80)), \
                patch.object(cli.usage, 'peak', return_value=10):
            skip, _ = self.capture(cli.over_ceiling, 'claude:p1', lambda: None, 'Claude p1')
        self.assertFalse(skip)


# ─── claude_with_fallback ────────────────────────────────────────

class ClaudeFallbackTests(Base):
    def test_first_profile_succeeds(self):
        with patch.object(cli.config_tool, 'enabled_profiles', return_value=['p1']), \
                patch.object(cli.config_tool, 'profiles', return_value={'profiles': {'p1': {}}}), \
                patch.object(cli.routing, 'cooldown_left', return_value=0), \
                patch.object(cli.routing, 'over_ceiling', return_value=False), \
                patch.object(cli.routing, 'run_claude', return_value=0):
            code, _ = self.capture(cli.claude_with_fallback, 'hi')
        self.assertEqual(code, 0)

    def test_cooldown_then_key_success(self):
        with patch.object(cli.config_tool, 'enabled_profiles', return_value=['p1']), \
                patch.object(cli.config_tool, 'profiles', return_value={'profiles': {'p1': {}}}), \
                patch.object(cli.config_tool, 'stored_keys', return_value=[('k1', 'sk-ant', 'salva')]), \
                patch.object(cli.routing, 'cooldown_left', side_effect=[60, 0]), \
                patch.object(cli.routing, 'over_ceiling', return_value=False), \
                patch.object(cli.routing, 'run_claude', return_value=0):
            code, text = self.capture(cli.claude_with_fallback, 'hi')
        self.assertEqual(code, 0)
        self.assertIn('cooldown', text)

    def test_all_rate_limited_falls_to_codex(self):
        with patch.object(cli.config_tool, 'enabled_profiles', return_value=['p1']), \
                patch.object(cli.config_tool, 'profiles', return_value={'profiles': {'p1': {}}}), \
                patch.object(cli.config_tool, 'stored_keys', return_value=[]), \
                patch.object(cli.routing, 'cooldown_left', return_value=0), \
                patch.object(cli.routing, 'over_ceiling', return_value=False), \
                patch.object(cli.routing, 'run_claude', return_value=cli.RATE_LIMITED), \
                patch.object(cli.routing, 'installed', side_effect=lambda t: t == 'codex'), \
                patch.object(cli.routing, 'run_codex', return_value=0):
            code, text = self.capture(cli.claude_with_fallback, 'hi')
        self.assertEqual(code, 0)
        self.assertIn('último recurso', text)

    def test_hard_error_returns_immediately(self):
        with patch.object(cli.config_tool, 'enabled_profiles', return_value=['p1']), \
                patch.object(cli.config_tool, 'profiles', return_value={'profiles': {'p1': {}}}), \
                patch.object(cli.routing, 'cooldown_left', return_value=0), \
                patch.object(cli.routing, 'over_ceiling', return_value=False), \
                patch.object(cli.routing, 'run_claude', return_value=42):
            code, _ = self.capture(cli.claude_with_fallback, 'hi')
        self.assertEqual(code, 42)

    def test_nothing_available(self):
        with patch.object(cli.config_tool, 'enabled_profiles', return_value=[]), \
                patch.object(cli.config_tool, 'stored_keys', return_value=[]), \
                patch.object(cli.routing, 'installed', return_value=False):
            code, text = self.capture(cli.claude_with_fallback, 'hi')
        self.assertEqual(code, 1)
        self.assertIn('Nenhuma ferramenta', text)


# ─── route ───────────────────────────────────────────────────────

class RouteTests(Base):
    def test_forced_tool(self):
        with patch.object(cli.routing, 'runners', return_value={'codex': lambda p: 7}):
            self.assertEqual(cli.route('x', 'codex'), 7)

    def test_complex_goes_to_claude(self):
        fakes = {'claude': lambda p: 0}
        with patch.object(cli.routing, 'runners', return_value=fakes), \
                patch.object(cli.routing, 'classify', return_value='complex'), \
                patch.object(cli.routing, 'emit'):
            code, text = self.capture(cli.route, 'x')
        self.assertEqual(code, 0)
        self.assertIn('complexa', text)

    def test_simple_uses_fallback_order(self):
        calls = []
        fakes = {'claude': lambda p: 0, 'codex': lambda p: calls.append('codex') or 0}
        with patch.object(cli.routing, 'runners', return_value=fakes), \
                patch.object(cli.routing, 'classify', return_value='simple'), \
                patch.object(cli.routing, 'config', return_value={'routing': {'prefer_tool_for_simple': 'codex',
                                                                      'fallback_order': []}}), \
                patch.object(cli.routing, 'installed', return_value=True), \
                patch.object(cli.routing, 'emit'):
            code, text = self.capture(cli.route, 'x')
        self.assertEqual(code, 0)
        self.assertEqual(calls, ['codex'])

    def test_simple_skips_and_final_claude(self):
        claude_called = []
        fakes = {'claude': lambda p: claude_called.append(1) or 0, 'deepseek': lambda p: 0,
                 'grok': lambda p: 0}
        with patch.object(cli.routing, 'runners', return_value=fakes), \
                patch.object(cli.routing, 'classify', return_value='simple'), \
                patch.object(cli.routing, 'config', return_value={'routing': {'prefer_tool_for_simple': 'deepseek',
                                                                      'fallback_order': ['grok']}}), \
                patch.object(cli.config_tool, 'stored_key', return_value=('', '')), \
                patch.object(cli.routing, 'installed', return_value=False), \
                patch.object(cli.routing, 'emit'):
            code, text = self.capture(cli.route, 'x')
        self.assertEqual(code, 0)
        self.assertTrue(claude_called)
        self.assertIn('fallback final', text)


# ─── version / update ────────────────────────────────────────────

class VersionUpdateTests(Base):
    def test_version_of_tool(self):
        done = MagicMock()
        done.stdout = 'codex 1.2.3\nextra\n'
        with patch.object(cli, 'resolve_bin', return_value='/bin/codex'), \
                patch.object(cli.subprocess, 'run', return_value=done):
            self.assertEqual(cli.version('codex'), 'codex 1.2.3')

    def test_version_handles_error(self):
        with patch.object(cli, 'resolve_bin', return_value='/bin/codex'), \
                patch.object(cli.subprocess, 'run', side_effect=OSError):
            self.assertEqual(cli.version('codex'), '?')

    def test_ai_version_reads_package(self):
        self.assertIsInstance(cli.ai_version(), str)

    def test_update_npm_path(self):
        with patch.object(cli.commands, 'REPO', Path(self.tmp.name)), \
                patch.dict(os.environ, {'SMART_AI_INSTALLER': 'npm'}), \
                patch.object(cli.commands, 'resolve_bin', return_value='/bin/npm'), \
                patch.object(cli.subprocess, 'call', return_value=0):
            code, text = self.capture(cli.update)
        self.assertEqual(code, 0)

    def test_update_no_git_prints_command(self):
        with patch.object(cli.commands, 'REPO', Path(self.tmp.name)), \
                patch.dict(os.environ, {'SMART_AI_INSTALLER': ''}, clear=False):
            os.environ.pop('SMART_AI_INSTALLER', None)
            code, text = self.capture(cli.update)
        self.assertEqual(code, 1)
        self.assertIn('atualizar', text)


# ─── status / usage / spend / history ────────────────────────────

class ReportTests(Base):
    def test_show_status(self):
        with patch.object(cli.config_tool, 'ordered',
                          return_value=[('p1', {'enabled': True, 'config_dir': '/x'})]), \
                patch.object(cli.config_tool, 'profiles', return_value={'profiles': {}}), \
                patch.object(cli.config_tool, 'KEYS', ['claude']), \
                patch.object(cli.config_tool, 'stored_key', return_value=('', '')), \
                patch.object(cli, 'cooldown_left', return_value=0), \
                patch.object(cli, 'installed', return_value=False), \
                patch.object(cli, 'version', return_value='1.0'):
            code, text = self.capture(cli.show_status)
        self.assertIn('Omni AI CLI', text)
        self.assertIn('p1', text)

    def test_render_usage_empty(self):
        with patch.object(cli.config_tool, 'ordered', return_value=[]), \
                patch.object(cli.config_tool, 'profiles', return_value={'profiles': {}}), \
                patch.object(cli.config_tool, 'stored_keys', return_value=[]), \
                patch.object(cli.config_tool, 'tool_accounts', return_value=[]), \
                patch.object(cli, 'installed', return_value=False), \
                patch.object(cli.usage, 'snapshot', return_value={}), \
                patch.object(cli.usage, 'thresholds', return_value=(90, 80)):
            code, text = self.capture(cli.show_usage, watch=False)
        self.assertEqual(code, 0)
        self.assertIn('Uso e limites', text)

    def test_spend_today_parses_rows(self):
        costs = self.log_file.with_name('costs.jsonl')
        costs.write_text(
            json.dumps({'at': '2026-01-01T10:00', 'tool': 'codex', 'cost': 0.5, 'tokens': 100}) + '\n' +
            json.dumps({'at': '2025-12-31', 'tool': 'codex', 'cost': 9}) + '\n' + 'badjson\n')
        totals = cli.spend_today('2026-01-01')
        self.assertIn('codex', totals)
        self.assertEqual(totals['codex'][0], 0.5)

    def test_spend_today_missing_file(self):
        self.assertEqual(cli.spend_today('2026-01-01'), {})

    def test_history_list_empty(self):
        with patch('omni_ai.history.sessions', return_value=[]):
            code, text = self.capture(cli.history_command, [])
        self.assertEqual(code, 0)
        self.assertIn('Nenhuma conversa', text)

    def test_history_list_items_and_source_filter(self):
        items = [{'source': 'omni', 'updated': 0, 'count': 2, 'title': 'T', 'id': 'abc'}]
        with patch('omni_ai.history.sessions', return_value=items) as sess:
            code, text = self.capture(cli.history_command, ['list', '--source', 'omni'])
        self.assertEqual(code, 0)
        self.assertIn('abc', text)
        sess.assert_called_once_with(['omni'])

    def test_history_conversation_found_and_json(self):
        conv = {'title': 'T', 'source': 'omni', 'cwd': '/x',
                'messages': [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'yo'}]}
        with patch('omni_ai.history.conversation', return_value=conv):
            code, text = self.capture(cli.history_command, ['abc'])
            self.assertEqual(code, 0)
            self.assertIn('hi', text)
            code, text = self.capture(cli.history_command, ['abc', '--json'])
            self.assertEqual(code, 0)
            self.assertIn('"title"', text)

    def test_history_conversation_missing(self):
        with patch('omni_ai.history.conversation', return_value=None):
            code, text = self.capture(cli.history_command, ['missing'])
            self.assertEqual(code, 1)
            code, text = self.capture(cli.history_command, ['missing', '--json'])
            self.assertEqual(code, 1)
            self.assertIn('null', text)


# ─── completions / doctor branches ───────────────────────────────

class CompletionsDoctorTests(Base):
    def test_completions_unsupported(self):
        code, text = self.capture(cli.completions, 'fish')
        self.assertEqual(code, 1)

    def test_doctor_local_bin_in_path(self):
        local_bin = str(Path('~/.local/bin').expanduser())
        with patch.dict(os.environ, {'PATH': local_bin}), \
                patch.object(cli.config_tool, 'ordered',
                             return_value=[('p1', {'enabled': True, 'config_dir': '/x'})]), \
                patch.object(cli.config_tool, 'profiles', return_value={'profiles': {}}), \
                patch.object(cli.config_tool, 'KEYS', ['claude', 'groq']), \
                patch.object(cli.config_tool, 'stored_key', return_value=('x', 'f')), \
                patch.object(cli, 'cooldown_left', return_value=0), \
                patch.object(cli, 'installed', return_value=True), \
                patch.object(cli, 'version', return_value='1.0'):
            code, text = self.capture(cli.doctor)
        self.assertEqual(code, 0)
        self.assertIn('no PATH', text)


# ─── main() dispatch ─────────────────────────────────────────────

class MainDispatchTests(Base):
    def test_version_flag(self):
        with patch.object(cli, 'ai_version', return_value='1.2.3'):
            code, text = self.main(['--version'])
        self.assertEqual(code, 0)
        self.assertIn('1.2.3', text)

    def test_help(self):
        code, text = self.main(['--help'])
        self.assertEqual(code, 0)
        self.assertIn('Uso', text)

    def test_empty_prompt_shows_usage(self):
        # no tty so no chat; empty words → usage, return 1
        with patch.object(cli.sys.stdin, 'isatty', return_value=False):
            code, text = self.main([])
        self.assertEqual(code, 1)

    def test_no_args_opens_chat_on_tty(self):
        with patch.object(cli.sys.stdin, 'isatty', return_value=True), \
                patch('omni_ai.chat.main', return_value=0) as chat_main:
            code, _ = self.main([])
        self.assertEqual(code, 0)
        chat_main.assert_called_once()

    def test_status(self):
        with patch.object(cli, 'show_status') as st:
            code, _ = self.main(['--status'])
        self.assertEqual(code, 0)
        st.assert_called_once()

    def test_doctor_dispatch(self):
        with patch.object(cli, 'doctor', return_value=0) as doc:
            self.assertEqual(self.main(['doctor'])[0], 0)
        doc.assert_called_once()

    def test_reset_and_log(self):
        with patch.object(cli, 'reset_cooldowns') as rc:
            self.assertEqual(self.main(['--reset'])[0], 0)
        rc.assert_called_once()
        with patch.object(cli, 'show_log') as sl:
            self.assertEqual(self.main(['--log'])[0], 0)
        sl.assert_called_once()

    def test_usage_flag(self):
        with patch.object(cli, 'show_usage', return_value=0) as su:
            self.assertEqual(self.main(['--usage'])[0], 0)
        su.assert_called_once_with(watch=False)
        with patch.object(cli, 'show_usage', return_value=0) as su:
            self.main(['--usage', 'watch'])
        su.assert_called_once_with(watch=True)

    def test_classify_flag(self):
        with patch.object(cli, 'classify', return_value='simple'):
            code, text = self.main(['--classify', 'what is x'])
        self.assertEqual(code, 0)
        self.assertIn('SIMPLES', text)

    def test_mode_valid_then_route(self):
        with patch.object(cli, 'route', return_value=0) as rt:
            code, _ = self.main(['--mode', 'plan', 'do it'])
        self.assertEqual(code, 0)
        self.assertEqual(os.environ.get('SMART_AI_MODE'), 'plan')
        rt.assert_called_once_with('do it', 'auto')

    def test_mode_invalid(self):
        code, _ = self.main(['--mode', 'bogus', 'x'])
        self.assertEqual(code, 1)

    def test_tool_flag_routes(self):
        with patch.object(cli, 'route', return_value=0) as rt:
            code, _ = self.main(['--claude', 'hello'])
        self.assertEqual(code, 0)
        rt.assert_called_once_with('hello', 'claude')

    def test_double_dash_passthrough(self):
        with patch.object(cli, 'route', return_value=0) as rt:
            self.main(['--', '--not-a-flag', 'word'])
        rt.assert_called_once_with('--not-a-flag word', 'auto')

    def test_completions_dispatch(self):
        code, text = self.main(['--completions', 'bash'])
        self.assertEqual(code, 0)
        self.assertIn('complete -F', text)

    def test_update_dispatch(self):
        with patch.object(cli, 'update', return_value=0) as up:
            self.assertEqual(self.main(['--update'])[0], 0)
        up.assert_called_once()

    def test_history_dispatch(self):
        with patch.object(cli, 'history_command', return_value=0) as hc:
            self.assertEqual(self.main(['history', 'list'])[0], 0)
        hc.assert_called_once_with(['list'])

    def test_sync_dispatch(self):
        with patch('omni_ai.sync.main', return_value=0) as sm:
            self.assertEqual(self.main(['--sync', '--dry-run'])[0], 0)
        sm.assert_called_once_with(['--dry-run'])

    def test_menu_json(self):
        with patch('omni_ai.skills.discover', return_value={'s1': 1}):
            code, text = self.main(['--menu-json'])
        self.assertEqual(code, 0)
        data = json.loads(text)
        self.assertIn('tools', data)
        self.assertIn('auto', data['tools'])
        self.assertEqual(data['skills'], ['s1'])

    def test_setup_and_install(self):
        with patch('omni_ai.onboarding.main') as ob:
            self.assertEqual(self.main(['--setup'])[0], 0)
            ob.assert_called_once_with(install_missing=False)
        with patch('omni_ai.onboarding.main') as ob:
            self.assertEqual(self.main(['--install'])[0], 0)
            ob.assert_called_once_with(install_missing=True)

    def test_chat_flags(self):
        with patch('omni_ai.chat.main', return_value=0) as cm:
            self.assertEqual(self.main(['--chat', 'x'])[0], 0)
            cm.assert_called_once_with(['x'])
        with patch('omni_ai.chat.main', return_value=0) as cm:
            self.assertEqual(self.main(['--resume'])[0], 0)
            cm.assert_called_once_with(['--resume'])

    def test_delegated_ok(self):
        with patch.object(cli.config_tool, 'main', return_value=None) as cm:
            self.assertEqual(self.main(['--model', 'claude', 'opus'])[0], 0)
        cm.assert_called_once()

    def test_delegated_systemexit(self):
        with patch.object(cli.config_tool, 'main', side_effect=SystemExit(2)):
            code, _ = self.main(['--key', 'openrouter'])
        self.assertEqual(code, 1)

    def test_delegated_systemexit_zero(self):
        with patch.object(cli.config_tool, 'main', side_effect=SystemExit(0)):
            code, _ = self.main(['--account', 'list'])
        self.assertEqual(code, 0)


class RenderUsageFullTests(Base):
    def test_render_usage_with_data(self):
        import time
        today = time.strftime('%Y-%m-%d')
        self.log_file.write_text(f'[{today}T10:00] tool=claude profile=p1 status=success\n')
        costs = self.log_file.with_name('costs.jsonl')
        costs.write_text(json.dumps({'at': today, 'tool': 'codex', 'cost': 0.25, 'tokens': 50}) + '\n')
        cli.set_state('limits', {'claude:p1': {'text': 'resets at 7pm', 'at': int(time.time()), 'until': None}})

        profiles = {'profiles': {'p1': {'enabled': True, 'name': 'Main'},
                                 'p2': {'enabled': False, 'name': 'Off'}}}
        ordered = [('p1', {'enabled': True, 'name': 'Main'}), ('p2', {'enabled': False, 'name': 'Off'})]
        now = time.time()
        data = {
            ('claude-who', 'p1'): {'email': 'a@b.com', 'plan': 'Pro'},
            ('claude', 'p1'): {'windows': [{'used': 50, 'minutes': 300, 'resets': int(now) + 3600, 'label': None}],
                               'at': now, 'blocked': False},
            ('codex', 'primary'): {'windows': [{'used': 100, 'minutes': 300, 'resets': None, 'label': 'sessão'}],
                                   'at': now, 'blocked': True, 'email': 'c@x', 'plan': 'Plus'},
            ('openrouter', 'principal'): {'used': 1.5, 'limit': 10},
        }
        or_keys = [('principal', 'sk-or-abcdefgh', 'salva')]

        def stored_keys(provider):
            return or_keys if provider == 'openrouter' else []

        with patch.object(cli.config_tool, 'ordered', return_value=ordered), \
                patch.object(cli.config_tool, 'profiles', return_value=profiles), \
                patch.object(cli.config_tool, 'stored_keys', side_effect=stored_keys), \
                patch.object(cli.config_tool, 'tool_accounts', return_value=[]), \
                patch.object(cli.config_tool, 'logged_in', return_value=True), \
                patch.object(cli.usage_view, 'installed', side_effect=lambda t: t == 'codex'), \
                patch.object(cli.usage_view, 'cooldown_left', return_value=0), \
                patch.object(cli.usage, 'snapshot', return_value=data), \
                patch.object(cli.usage, 'thresholds', return_value=(90, 80)), \
                patch.object(cli.usage, 'peak', side_effect=lambda w: max((x['used'] for x in w), default=0)), \
                patch.object(cli.usage, 'bar', side_effect=lambda used, width=20: '█' * int(used * width / 100)), \
                patch.object(cli.usage, 'window_label', return_value='5h'), \
                patch.object(cli.usage, 'resets_in', return_value='1h'), \
                patch.object(cli.usage, 'clock', return_value='19:00'), \
                patch.object(cli.usage, 'load_cache', return_value={}):
            code, text = self.capture(cli.render_usage)
        self.assertEqual(code, 0)
        self.assertIn('a@b.com', text)
        self.assertIn('Gasto de hoje', text)
        self.assertIn('no limite', text)

    def test_show_usage_watch_loop(self):
        with patch.object(cli.usage_view, 'render_usage', return_value=0), \
                patch.object(cli.time, 'sleep', side_effect=KeyboardInterrupt), \
                patch.object(cli.sys.stdout, 'write'), patch.object(cli.sys.stdout, 'flush'):
            code, _ = self.capture(cli.show_usage, watch=True)
        self.assertEqual(code, 0)


class KeyRetryBranchTests(Base):
    def test_account_cooldown_ceiling_and_extra_account(self):
        accounts = [('acct_cd', {}), ('acct_ok', {})]
        cooldowns = {'codex:acct_cd': 60}
        with patch.object(cli.config_tool, 'tool_accounts', return_value=accounts), \
                patch.object(cli.config_tool, 'tool_env', return_value={}), \
                patch.object(cli.config_tool, 'stored_keys', return_value=[]), \
                patch.object(cli.executors, 'cooldown_left', side_effect=lambda a: cooldowns.get(a, 0)), \
                patch.object(cli.executors, 'over_ceiling', return_value=False), \
                patch.object(cli.executors, 'run', side_effect=[(1, 'limit reached'), (0, '')]):
            code, text = self.capture(cli.run_with_key_retry, 'codex', ['codex'], 'CODEX_API_KEY', 'Codex')
        self.assertEqual(code, 0)
        self.assertIn('cooldown', text)
        self.assertIn('conta acct_ok', text)

    def test_codex_ceiling_skips_account(self):
        with patch.object(cli.config_tool, 'tool_accounts', return_value=[('acct2', {})]), \
                patch.object(cli.config_tool, 'tool_env', return_value={}), \
                patch.object(cli.config_tool, 'stored_keys', return_value=[]), \
                patch.object(cli.executors, 'cooldown_left', return_value=0), \
                patch.object(cli.executors, 'over_ceiling', side_effect=lambda key, fetch, label: 'acct2' in key), \
                patch.object(cli.executors, 'run', return_value=(0, '')):
            code, _ = self.capture(cli.run_with_key_retry, 'codex', ['codex'], 'CODEX_API_KEY', 'Codex')
        self.assertEqual(code, 0)

    def test_env_key_skipped_and_saved_key_rate_limited(self):
        with patch.object(cli.config_tool, 'tool_accounts', return_value=[]), \
                patch.object(cli.config_tool, 'tool_env', return_value={}), \
                patch.object(cli.config_tool, 'stored_keys',
                             return_value=[('env', 'v', 'env'), ('k1', 'v', 'salva')]), \
                patch.object(cli.executors, 'cooldown_left', return_value=0), \
                patch.object(cli.executors, 'over_ceiling', return_value=False), \
                patch.object(cli.executors, 'run', side_effect=[(1, 'nope'), (1, 'rate limit reached')]):
            code, text = self.capture(cli.run_with_key_retry, 'cursor', ['cursor'], 'CURSOR_API_KEY', 'Cursor')
        self.assertEqual(code, 1)
        self.assertIn('chave k1', text)


class UpdateGitTests(Base):
    def test_update_git_pull(self):
        (Path(self.tmp.name) / '.git').mkdir()
        done = MagicMock()
        done.stdout = 'main\n'
        with patch.object(cli.commands, 'REPO', Path(self.tmp.name)), \
                patch.object(cli.commands, 'resolve_bin', return_value='git'), \
                patch.object(cli.subprocess, 'run', return_value=done), \
                patch.object(cli.subprocess, 'call', return_value=0):
            code, text = self.capture(cli.update)
        self.assertEqual(code, 0)
        self.assertIn('atualizado', text)


class CompletionsZshTests(Base):
    def test_completions_zsh(self):
        code, text = self.capture(cli.completions, 'zsh')
        self.assertEqual(code, 0)
        self.assertIn('compdef _ai ai', text)


class DoctorBranchTests(Base):
    def test_mixed_profiles(self):
        import time
        cli.set_state('busy_last_rate_limit', str(int(time.time())))
        ordered = [('disabled', {'enabled': False}),
                   ('busy', {'enabled': True, 'config_dir': '/x'}),
                   ('nologin', {'enabled': True})]
        with patch.object(cli.config_tool, 'ordered', return_value=ordered), \
                patch.object(cli.config_tool, 'profiles', return_value={'profiles': {}}), \
                patch.object(cli.config_tool, 'KEYS', ['claude']), \
                patch.object(cli.config_tool, 'stored_key', return_value=('', '')), \
                patch.object(cli.health, 'cooldown_left', side_effect=lambda n: 120 if n == 'busy' else 0), \
                patch.object(cli.health, 'installed', return_value=False), \
                patch.object(cli.health, 'version', return_value='1.0'):
            code, text = self.capture(cli.doctor)
        self.assertEqual(code, 0)  # 'busy' counts as logged, so the router can still answer
        self.assertIn('desabilitado', text)
        self.assertIn('cooldown', text)
        self.assertIn('sem login', text)


class TrivialHelperTests(Base):
    def test_config_and_profile_conf(self):
        with patch.object(cli.config_tool, 'read_json', return_value={'profiles': {'p1': {'name': 'X'}}}), \
                patch.object(cli.config_tool, 'profiles_path', return_value='/x'):
            self.assertEqual(cli.config()['profiles']['p1']['name'], 'X')
            self.assertEqual(cli.profile_conf('p1'), {'name': 'X'})

    def test_models(self):
        with patch.object(cli.config_tool, 'model_values', return_value=MODELS):
            self.assertEqual(cli.models(), MODELS)

    def test_installed(self):
        with patch.object(cli.base, 'resolve_bin', return_value='/bin/x'):
            self.assertTrue(cli.installed('codex'))
        with patch.object(cli.base, 'resolve_bin', return_value=None):
            self.assertFalse(cli.installed('codex'))


class RenderUsageAltBranchTests(Base):
    def test_no_login_and_sem_teto_and_key_cooldown(self):
        import time
        now = time.time()
        ordered = [('p1', {'enabled': True, 'name': 'Main'})]
        profiles = {'profiles': {'p1': {'enabled': True, 'name': 'Main'}}}
        # No live reading, logged_in False → 'sem login' line (966-967)
        data = {('openrouter', 'principal'): {'used': 2.0, 'limit': 0}}  # sem teto (997-1000)
        or_keys = [('principal', 'sk-or-abcdefgh', 'salva')]
        cli.set_state('limits', {'claude:p1': {'text': 'resets in 1h', 'at': int(now), 'until': int(now) + 3600}})

        def stored_keys(provider):
            return or_keys if provider == 'openrouter' else \
                ([('k1', 'val12345', 'salva')] if provider == 'claude' else [])

        def cooldown(account):
            return 120 if account == 'api-key' else 0  # claude saved key in cooldown (924-928)

        with patch.object(cli.config_tool, 'ordered', return_value=ordered), \
                patch.object(cli.config_tool, 'profiles', return_value=profiles), \
                patch.object(cli.config_tool, 'stored_keys', side_effect=stored_keys), \
                patch.object(cli.config_tool, 'tool_accounts', return_value=[]), \
                patch.object(cli.config_tool, 'logged_in', return_value=False), \
                patch.object(cli.usage_view, 'installed', return_value=False), \
                patch.object(cli.usage_view, 'cooldown_left', side_effect=cooldown), \
                patch.object(cli.usage, 'snapshot', return_value=data), \
                patch.object(cli.usage, 'thresholds', return_value=(90, 80)), \
                patch.object(cli.usage, 'peak', side_effect=lambda w: max((x['used'] for x in w), default=0)), \
                patch.object(cli.usage, 'bar', side_effect=lambda used, width=20: '█' * int(used * width / 100)), \
                patch.object(cli.usage, 'load_cache', return_value={}):
            code, text = self.capture(cli.render_usage)
        self.assertEqual(code, 0)
        self.assertIn('sem login', text)
        self.assertIn('sem teto', text)


class EntryTests(Base):
    def test_entry_keyboardinterrupt(self):
        with patch.object(cli, 'main', side_effect=KeyboardInterrupt):
            self.assertEqual(cli.entry(), 130)


if __name__ == '__main__':
    unittest.main()
