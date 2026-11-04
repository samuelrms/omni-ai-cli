import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)
sys.path.insert(0, str(ROOT))
from omni_ai import sync


class FakeProc:
    def __init__(self, returncode=0, stderr='', stdout=''):
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


class PureHelperTests(unittest.TestCase):
    def test_normalize_http_url(self):
        self.assertEqual(sync.normalize({'url': 'https://x/mcp', 'headers': {'A': 'b'}}),
                         {'url': 'https://x/mcp', 'headers': {'A': 'b'}})

    def test_normalize_server_url_and_http_headers_aliases(self):
        self.assertEqual(sync.normalize({'serverUrl': 'https://y/mcp', 'http_headers': {'K': 'v'}}),
                         {'url': 'https://y/mcp', 'headers': {'K': 'v'}})

    def test_normalize_stdio(self):
        self.assertEqual(sync.normalize({'command': 'npx', 'args': ['-y', 'pkg'], 'env': {'T': '1'}}),
                         {'command': 'npx', 'args': ['-y', 'pkg'], 'env': {'T': '1'}})

    def test_normalize_stdio_defaults(self):
        self.assertEqual(sync.normalize({'command': 'node'}),
                         {'command': 'node', 'args': [], 'env': {}})

    def test_normalize_unknown_returns_none(self):
        self.assertIsNone(sync.normalize({'foo': 'bar'}))

    def test_identity_url_strips_trailing_slash(self):
        self.assertEqual(sync.identity({'url': 'https://x/mcp/'}), 'https://x/mcp')

    def test_identity_runner_uses_package_arg(self):
        ident = sync.identity({'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-filesystem', '/a']})
        self.assertEqual(ident, 'filesystem')

    def test_identity_plain_binary_strips_prefix_and_suffix(self):
        self.assertEqual(sync.identity({'command': '/usr/local/bin/mcp-server-filesystem', 'args': []}),
                         'filesystem')

    def test_identity_strips_version_suffix(self):
        self.assertEqual(sync.identity({'command': 'uvx', 'args': ['some-tool@1.2.3']}), 'some-tool')

    def test_app_internal_detects_app_bundle(self):
        self.assertTrue(sync.app_internal({'command': '/Applications/Codex.app/Contents/bin/repl'}))

    def test_app_internal_detects_relative_command(self):
        self.assertTrue(sync.app_internal({'command': './local-bin'}))

    def test_app_internal_false_for_normal(self):
        self.assertFalse(sync.app_internal({'command': 'npx'}))

    def test_read_json_valid_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / 'a.json'
            good.write_text('{"x": 1}')
            self.assertEqual(sync.read_json(good), {'x': 1})
            self.assertEqual(sync.read_json(Path(d) / 'missing.json'), {})
            bad = Path(d) / 'bad.json'
            bad.write_text('{not json')
            self.assertEqual(sync.read_json(bad), {})


class AddCommandTests(unittest.TestCase):
    def setUp(self):
        self._bins = patch.object(sync, 'BINS', {'claude': 'claude', 'codex': 'codex', 'agy': 'agy'})
        self._bins.start()
        self.addCleanup(self._bins.stop)

    def test_http_claude_uses_add_json(self):
        argv = sync.add_command('claude', 'docs', {'url': 'https://d/mcp', 'headers': {}})
        self.assertEqual(argv[:5], ['claude', 'mcp', 'add-json', '-s', 'user'])
        self.assertIn('docs', argv)
        self.assertIn('"type": "http"', argv[-1])

    def test_http_codex_without_headers(self):
        argv = sync.add_command('codex', 'docs', {'url': 'https://d/mcp', 'headers': {}})
        self.assertEqual(argv, ['codex', 'mcp', 'add', 'docs', '--url', 'https://d/mcp'])

    def test_http_codex_with_headers_unsupported(self):
        self.assertIsNone(sync.add_command('codex', 'docs', {'url': 'https://d/mcp', 'headers': {'A': 'b'}}))

    def test_http_agy_includes_headers(self):
        argv = sync.add_command('agy', 'docs', {'url': 'https://d/mcp', 'headers': {'A': 'b'}})
        self.assertEqual(argv, ['agy', 'mcp', 'add', '-H', 'A: b', 'docs', 'https://d/mcp'])

    def test_stdio_claude_add_json(self):
        argv = sync.add_command('claude', 'fs', {'command': 'npx', 'args': ['-y', 'pkg'], 'env': {}})
        self.assertIn('"type": "stdio"', argv[-1])

    def test_stdio_codex_uses_env_flag(self):
        argv = sync.add_command('codex', 'fs', {'command': 'npx', 'args': ['x'], 'env': {'T': '1'}})
        self.assertEqual(argv, ['codex', 'mcp', 'add', '--env', 'T=1', 'fs', '--', 'npx', 'x'])

    def test_stdio_agy_uses_dash_e_flag(self):
        argv = sync.add_command('agy', 'fs', {'command': 'npx', 'args': ['x'], 'env': {'T': '1'}})
        self.assertEqual(argv, ['agy', 'mcp', 'add', '-e', 'T=1', 'fs', '--', 'npx', 'x'])


class ReadersTests(unittest.TestCase):
    def _home(self):
        d = tempfile.mkdtemp()
        import shutil
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        home = Path(d)
        p = patch.object(sync, 'HOME', home)
        p.start()
        self.addCleanup(p.stop)
        return home

    def test_servers_claude_reads_and_normalizes(self):
        home = self._home()
        (home / '.claude.json').write_text(json.dumps({'mcpServers': {
            'docs': {'url': 'https://d/mcp'}}}))
        self.assertEqual(sync.servers_claude(), {'docs': {'url': 'https://d/mcp', 'headers': {}}})

    def test_servers_codex_parses_toml(self):
        home = self._home()
        (home / '.codex').mkdir()
        (home / '.codex' / 'config.toml').write_text(
            '[mcp_servers.docs]\nurl = "https://d/mcp"\n')
        self.assertEqual(sync.servers_codex(), {'docs': {'url': 'https://d/mcp', 'headers': {}}})

    def test_servers_codex_missing_file_returns_empty(self):
        self._home()
        self.assertEqual(sync.servers_codex(), {})

    def test_servers_agy_reads_config(self):
        home = self._home()
        (home / '.gemini' / 'config').mkdir(parents=True)
        (home / '.gemini' / 'config' / 'mcp_config.json').write_text(json.dumps({'mcpServers': {
            'fs': {'command': 'npx', 'args': ['x']}}}))
        self.assertEqual(sync.servers_agy(), {'fs': {'command': 'npx', 'args': ['x'], 'env': {}}})


class SkillsTests(unittest.TestCase):
    def test_skills_by_tool_finds_valid_skills(self):
        d = tempfile.mkdtemp()
        import shutil
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        home = Path(d)
        claude_dir = home / '.claude' / 'skills'
        (claude_dir / 'humanizer').mkdir(parents=True)
        (claude_dir / 'humanizer' / 'SKILL.md').write_text('x')
        # Hidden folders and folders without SKILL.md are ignored.
        (claude_dir / '.hidden').mkdir()
        (claude_dir / 'nope').mkdir()
        with patch.object(sync, 'SKILL_DIRS', {'claude': [claude_dir], 'codex': [], 'agy': []}):
            found = sync.skills_by_tool()
        self.assertIn('humanizer', found['claude'])
        self.assertNotIn('nope', found['claude'])
        self.assertNotIn('.hidden', found['claude'])
        self.assertEqual(found['codex'], {})


class InstalledTests(unittest.TestCase):
    def test_installed_true_and_false(self):
        with patch.object(sync, 'BINS', {'claude': 'claude'}):
            with patch.object(sync, 'resolve_bin', return_value='/bin/claude'):
                self.assertTrue(sync.installed('claude'))
            with patch.object(sync, 'resolve_bin', return_value=None):
                self.assertFalse(sync.installed('claude'))


class PlanAndMainTests(unittest.TestCase):
    """Drive plan()/main() in-process with a fake home and patched side effects."""

    def _setup(self, claude_json=None, codex_toml=None, agy_json=None, skills=None,
               installed=('claude', 'codex', 'agy')):
        import shutil
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        home = Path(d)
        if claude_json is not None:
            (home / '.claude.json').write_text(json.dumps(claude_json))
        if codex_toml is not None:
            (home / '.codex').mkdir()
            (home / '.codex' / 'config.toml').write_text(codex_toml)
        if agy_json is not None:
            (home / '.gemini' / 'config').mkdir(parents=True)
            (home / '.gemini' / 'config' / 'mcp_config.json').write_text(json.dumps(agy_json))

        skill_dirs = {
            'claude': [home / '.claude' / 'skills'],
            'codex': [home / '.agents' / 'skills', home / '.codex' / 'skills'],
            'agy': [home / '.gemini' / 'antigravity' / 'skills'],
        }
        for tool, name in (skills or []):
            folder = skill_dirs[tool][0] / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'SKILL.md').write_text('x')

        bins = {'claude': 'claude', 'codex': 'codex', 'agy': 'agy'}

        for name, val in {'HOME': home, 'SKILL_DIRS': skill_dirs, 'BINS': bins}.items():
            p = patch.object(sync, name, val)
            p.start()
            self.addCleanup(p.stop)

        def fake_resolve(binname):
            tool = {'claude': 'claude', 'codex': 'codex', 'agy': 'agy'}.get(binname)
            if tool and tool in installed:
                return '/fake/' + binname
            # unknown binaries (module_cmd etc.) resolve normally
            return '/fake/' + binname if binname not in bins.values() else None
        rp = patch.object(sync, 'resolve_bin', side_effect=fake_resolve)
        rp.start()
        self.addCleanup(rp.stop)
        return home

    def test_plan_dedupe_skip_and_skills(self):
        self._setup(
            claude_json={'mcpServers': {'files': {'command': 'npx',
                         'args': ['-y', '@modelcontextprotocol/server-filesystem', '/a']}}},
            codex_toml='[mcp_servers.repl]\ncommand = "/Applications/Codex.app/Contents/bin/repl"\n'
                       '[mcp_servers.docs]\nurl = "https://docs.example/mcp"\n',
            agy_json={'mcpServers': {'fs': {'command': '/usr/local/bin/mcp-server-filesystem',
                      'args': ['/a']}}},
            skills=[('claude', 'humanizer')],
        )
        tools, mcp_actions, skipped, skill_actions = sync.plan()
        self.assertEqual(set(tools), {'claude', 'codex', 'agy'})
        targets = {(t, n) for t, n, *_ in mcp_actions}
        # docs must be propagated to claude and agy; files to codex and agy (not claude where it lives).
        self.assertIn(('claude', 'docs'), targets)
        self.assertIn(('agy', 'docs'), targets)
        self.assertIn(('codex', 'files'), targets)
        # fs is the same filesystem server as files -> deduped, never re-added anywhere.
        self.assertFalse(any(n == 'fs' for _t, n, *_ in mcp_actions))
        # repl is app-internal -> never surfaces.
        self.assertFalse(any(n == 'repl' for _t, n, *_ in mcp_actions))
        skill_targets = {(t, n) for t, n, *_ in skill_actions}
        self.assertIn(('codex', 'humanizer'), skill_targets)
        self.assertIn(('agy', 'humanizer'), skill_targets)

    def _run_main(self, argv, inputs=None):
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            # MCP add commands: the first is told to fail to exercise the failure branch.
            if 'add-json' in cmd or ('mcp' in cmd and 'add' in cmd):
                return FakeProc(returncode=1 if len(calls) == 1 else 0, stderr='nope')
            return FakeProc(returncode=0)

        out = io.StringIO()
        with patch.object(sync.subprocess, 'run', side_effect=fake_run), \
                patch.object(sync, 'link') as link_mock, \
                contextlib.redirect_stdout(out):
            if inputs is not None:
                with patch('builtins.input', side_effect=inputs):
                    code = sync.main(argv)
            else:
                code = sync.main(argv)
        return code, out.getvalue(), calls, link_mock

    def test_main_nothing_to_sync(self):
        self._setup(
            claude_json={'mcpServers': {'docs': {'url': 'https://d/mcp'}}},
            codex_toml='[mcp_servers.docs]\nurl = "https://d/mcp"\n',
            agy_json={'mcpServers': {'docs': {'url': 'https://d/mcp'}}},
            installed=('claude', 'codex', 'agy'),
        )
        code, out, calls, _ = self._run_main([])
        self.assertEqual(code, 0)
        self.assertIn('Tudo sincronizado', out)

    def test_main_dry_run_shows_plan_without_applying(self):
        self._setup(
            claude_json={'mcpServers': {'docs': {'url': 'https://d/mcp'}}},
            codex_toml='',
            agy_json={'mcpServers': {}},
            skills=[('claude', 'humanizer')],
        )
        code, out, calls, link_mock = self._run_main(['--dry-run'])
        self.assertEqual(code, 0)
        self.assertIn('MCP', out)
        self.assertIn('skill', out)
        self.assertEqual(calls, [])  # nothing executed
        link_mock.assert_not_called()

    def test_main_apply_with_yes_flag_reports_failure(self):
        self._setup(
            claude_json={'mcpServers': {'docs': {'url': 'https://d/mcp'},
                         'fsx': {'command': 'npx', 'args': ['-y', 'pkgx'], 'env': {'TOK': 's'}}}},
            codex_toml='',
            agy_json={'mcpServers': {}},
            skills=[('claude', 'humanizer')],
        )
        code, out, calls, link_mock = self._run_main(['--yes'])
        # One MCP add was forced to fail -> non-zero exit.
        self.assertEqual(code, 1)
        self.assertIn('✗', out)
        self.assertIn('Atenção', out)  # env-carrying server warning
        link_mock.assert_called()  # skills linked
        # The trailing account sync-all call happened too.
        self.assertTrue(any('account' in c for c in calls))

    def test_main_skill_link_failure_is_reported(self):
        self._setup(
            claude_json={'mcpServers': {}},
            codex_toml='',
            agy_json={'mcpServers': {}},
            skills=[('claude', 'humanizer')],
        )

        def fake_run(cmd, *a, **k):
            return FakeProc(returncode=0)

        out = io.StringIO()
        with patch.object(sync.subprocess, 'run', side_effect=fake_run), \
                patch.object(sync, 'link', side_effect=OSError('permission denied')), \
                contextlib.redirect_stdout(out):
            code = sync.main(['--yes'])
        self.assertEqual(code, 1)
        self.assertIn('✗ skill', out.getvalue())
        self.assertIn('permission denied', out.getvalue())

    def test_main_prompt_declined(self):
        self._setup(
            claude_json={'mcpServers': {'docs': {'url': 'https://d/mcp'}}},
            codex_toml='',
            agy_json={'mcpServers': {}},
        )
        code, out, calls, link_mock = self._run_main([], inputs=['n'])
        self.assertEqual(code, 0)
        self.assertIn('Nada alterado', out)
        self.assertEqual(calls, [])

    def test_main_prompt_accepted(self):
        self._setup(
            claude_json={'mcpServers': {'docs': {'url': 'https://d/mcp'}}},
            codex_toml='',
            agy_json={'mcpServers': {}},
        )
        code, out, calls, link_mock = self._run_main([], inputs=['s'])
        # docs added to codex and agy; first forced failure -> exit 1.
        self.assertEqual(code, 1)
        self.assertTrue(any('docs' in c for c in calls))

    def test_main_prompt_eof_aborts(self):
        self._setup(
            claude_json={'mcpServers': {'docs': {'url': 'https://d/mcp'}}},
            codex_toml='',
            agy_json={'mcpServers': {}},
        )
        code, out, calls, _ = self._run_main([], inputs=EOFError())
        self.assertEqual(code, 0)
        self.assertIn('Nada alterado', out)

    def test_main_skipped_http_with_headers(self):
        self._setup(
            claude_json={'mcpServers': {}},
            codex_toml='',
            agy_json={'mcpServers': {'sec': {'url': 'https://s/mcp', 'headers': {'A': 'b'}}}},
            skills=[('agy', 'humanizer')],
            installed=('codex', 'agy'),
        )
        # codex cannot express an HTTP server with headers -> it is "pula"(skipped).
        code, out, calls, _ = self._run_main(['--dry-run'])
        self.assertEqual(code, 0)
        self.assertIn('pula', out)


if __name__ == '__main__':
    unittest.main()
