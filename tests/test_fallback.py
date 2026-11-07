"""The fallback tries each account and tool once, and goes on after any ordinary failure."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import io
import json
import os
import unittest
from unittest.mock import patch

from omni_ai import cli
from omni_ai.cli import executors, limits, routing

LIMITED, NOT_READY = cli.RATE_LIMITED, cli.NOT_READY


class Base(unittest.TestCase):
    def setUp(self):
        self.calls = []
        for target, name, value in ((routing, 'over_ceiling', lambda *a: False), (routing, 'cooldown_left', lambda _: 0),
                                    (routing, 'installed', lambda _: True)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch('sys.stdout', io.StringIO())
        self.out = patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('run_codex', 'run_agy'):
            patcher = patch.object(routing, name, side_effect=self.tool(name[4:], 1))
            patcher.start()
            self.addCleanup(patcher.stop)

    def tool(self, name, code):
        def run(_prompt):
            self.calls.append(name)
            return code
        return run

    def fallback(self, claude, profiles=('main',), keys=(), **options):
        def run_claude(profile, _prompt):
            self.calls.append(profile)
            return claude[profile]
        with patch.object(routing, 'run_claude', run_claude), \
                patch.object(routing.config_tool, 'enabled_profiles', return_value=list(profiles)), \
                patch.object(routing.config_tool, 'profiles', return_value={'profiles': {}}), \
                patch.object(routing.config_tool, 'stored_keys', return_value=list(keys)):
            return routing.claude_with_fallback('p', **options)


class OrdinaryFailures(Base):
    def test_a_failure_goes_on_to_the_next_account(self):
        self.assertEqual(self.fallback({'a': 1, 'b': 0}, ('a', 'b')), 0)
        self.assertEqual(self.calls, ['a', 'b'])

    def test_a_failure_goes_on_to_the_saved_key(self):
        self.assertEqual(self.fallback({'a': 1, 'api-key': 0}, ('a',), [('principal', 'k', 'salva')]), 0)
        self.assertEqual(self.calls, ['a', 'api-key'])

    def test_a_failure_goes_on_to_the_other_tools(self):
        self.assertEqual(self.fallback({'a': 1}, ('a',)), 1)
        self.assertEqual(self.calls, ['a', 'codex', 'agy'])

    def test_the_exit_code_of_the_failure_is_kept(self):
        self.assertEqual(self.fallback({'a': 42}, ('a',)), 42)

    def test_a_failure_is_not_described_as_a_limit(self):
        self.fallback({'a': 1}, ('a',))
        self.assertNotIn('rate limit', self.out.getvalue())

    def test_only_limits_are_described_as_limits(self):
        self.fallback({'a': LIMITED}, ('a',))
        self.assertIn('rate limit', self.out.getvalue())

    def test_exit_code_2_is_an_ordinary_failure(self):
        self.fallback({'a': 2}, ('a',))
        self.assertNotIn('rate limit', self.out.getvalue())

    def test_a_user_interruption_stops_everything(self):
        for code in (130, -2, 143, -15):
            with self.subTest(code=code):
                self.calls.clear()
                self.assertEqual(self.fallback({'a': code, 'b': 0}, ('a', 'b')), code)
                self.assertEqual(self.calls, ['a'])

    def test_codes_of_the_fallback_are_outside_what_a_process_can_return(self):
        self.assertGreater(min(LIMITED, NOT_READY), 255)
        self.assertNotEqual(LIMITED, NOT_READY)


class Keys(Base):
    def test_each_key_has_its_own_account(self):
        self.assertNotEqual(executors.key_account('', 'env'), executors.key_account('', 'principal'))
        self.assertEqual(executors.key_account('', 'principal'), 'api-key')
        self.assertEqual(executors.key_account('', 'extra'), 'api-key:extra')
        self.assertEqual(executors.key_account('codex', 'principal'), 'codex:api-key')

    def test_variable_and_saved_key_run_once_each(self):
        keys = [('env', 'from-env', 'env'), ('principal', 'saved', 'salva')]
        self.fallback({'api-key:env': LIMITED, 'api-key': LIMITED}, (), keys)
        self.assertEqual(self.calls[:2], ['api-key:env', 'api-key'])

    def test_the_same_key_twice_runs_once(self):
        keys = [('env', 'same', 'env'), ('principal', 'same', 'salva')]
        self.fallback({'api-key:env': LIMITED, 'api-key': LIMITED}, (), keys)
        self.assertEqual(self.calls[:2], ['api-key:env', 'codex'])

    def test_run_claude_uses_the_key_that_was_asked_for(self):
        keys = [('env', 'from-env', 'env'), ('principal', 'saved', 'salva'), ('extra', 'other', 'salva')]
        for profile, wanted in (('api-key', 'saved'), ('api-key:env', 'from-env'), ('api-key:extra', 'other')):
            with self.subTest(profile=profile), patch.object(executors.config_tool, 'stored_keys', return_value=keys), \
                    patch.object(executors, 'installed', return_value=True), patch.object(executors, 'emit_provider'), \
                    patch.object(executors, 'header'), patch.object(executors, 'restore_credentials'), \
                    patch.object(executors, 'log_usage'), patch.object(executors, 'remember_limit'), \
                    patch.object(executors, 'models', return_value={'claude': 'm'}), \
                    patch.object(executors, 'run', return_value=(0, '')) as run:
                executors.run_claude(profile, 'p')
                self.assertEqual(run.call_args.args[1]['ANTHROPIC_API_KEY'], wanted)


class SimpleRoute(Base):
    def test_a_tool_that_already_failed_is_not_run_again(self):
        forced = {'claude': routing.claude_with_fallback, 'codex': self.tool('codex', 1), 'agy': self.tool('agy', 1)}
        with patch.object(routing, 'runners', return_value=forced), patch.object(routing, 'classify', return_value='simple'), \
                patch.object(routing, 'config', return_value={'routing': {'prefer_tool_for_simple': 'codex', 'fallback_order': ['agy']}}), \
                patch.object(routing, 'run_claude', side_effect=lambda profile, _p: self.calls.append(profile) or LIMITED), \
                patch.object(routing.config_tool, 'enabled_profiles', return_value=['main']), \
                patch.object(routing.config_tool, 'profiles', return_value={'profiles': {}}), \
                patch.object(routing.config_tool, 'stored_keys', return_value=[]):
            routing.route('p')
        self.assertEqual(self.calls, ['codex', 'agy', 'main'])

    def test_claude_alone_still_tries_the_others(self):
        self.fallback({'a': LIMITED}, ('a',))
        self.assertEqual(self.calls, ['a', 'codex', 'agy'])

    def test_a_tool_that_was_not_tried_still_runs(self):
        self.fallback({'a': LIMITED}, ('a',), tried=('codex',))
        self.assertEqual(self.calls, ['a', 'agy'])


class FinishedAnswers(unittest.TestCase):
    RESULT = json.dumps({'type': 'result', 'is_error': False, 'result': 'done'})
    TEXT = 'You have used 90% of your session limit'

    def events(self):
        patcher = patch.dict(os.environ, {'SMART_AI_EVENTS': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_clean_result_is_not_a_limit(self):
        self.events()
        self.assertFalse(limits.hit_limit(f'{self.TEXT}\n{self.RESULT}\n', 0))

    def test_an_error_result_is_a_limit(self):
        self.events()
        output = json.dumps({'type': 'result', 'is_error': True, 'result': 'You hit your usage limit'})
        self.assertTrue(limits.hit_limit(output, 1))

    def test_a_clean_exit_with_no_result_still_reads_the_text(self):
        self.events()
        self.assertTrue(limits.hit_limit(f'{self.TEXT}\n', 0))

    def test_a_failure_after_a_clean_result_still_counts(self):
        self.events()
        self.assertTrue(limits.hit_limit(f'{self.TEXT}\n{self.RESULT}\n', 1))


if __name__ == '__main__':
    unittest.main()
