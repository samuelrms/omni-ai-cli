"""Branch coverage for omni_ai.onboarding: login probes, the guide, installer and first-run.

resolve_bin, subprocess and input() are stubbed; the first-run marker uses a temp
file, so no installer ever runs and nothing persists on the machine.
"""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import onboarding  # noqa: E402


class FakeRun:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class LoggedInTests(unittest.TestCase):
    def test_claude_reads_json(self):
        with patch.object(onboarding, 'resolve_bin', return_value='claude'), \
                patch.object(onboarding.subprocess, 'run', return_value=FakeRun(0, '{"loggedIn": true}')):
            self.assertTrue(onboarding.logged_in('claude'))
        with patch.object(onboarding, 'resolve_bin', return_value='claude'), \
                patch.object(onboarding.subprocess, 'run', return_value=FakeRun(0, 'garbage')):
            self.assertFalse(onboarding.logged_in('claude'))

    def test_codex_uses_returncode(self):
        with patch.object(onboarding, 'resolve_bin', return_value='codex'), \
                patch.object(onboarding.subprocess, 'run', return_value=FakeRun(0)):
            self.assertTrue(onboarding.logged_in('codex'))

    def test_cursor_reads_text(self):
        with patch.object(onboarding, 'resolve_bin', return_value='cursor-agent'), \
                patch.object(onboarding.subprocess, 'run', return_value=FakeRun(0, 'Logged in as x')):
            self.assertTrue(onboarding.logged_in('cursor'))
        with patch.object(onboarding, 'resolve_bin', return_value='cursor-agent'), \
                patch.object(onboarding.subprocess, 'run', return_value=FakeRun(0, 'You are not logged in')):
            self.assertFalse(onboarding.logged_in('cursor'))

    def test_file_based_tools(self):
        with tempfile.TemporaryDirectory() as d:
            token = Path(d) / 'auth.json'
            with patch.object(onboarding.Path, 'expanduser', lambda self: token):
                self.assertFalse(onboarding.logged_in('grok'))
                token.write_text('{}')
                self.assertTrue(onboarding.logged_in('grok'))

    def test_exception_is_false(self):
        with patch.object(onboarding, 'resolve_bin', return_value='claude'), \
                patch.object(onboarding.subprocess, 'run', side_effect=OSError):
            self.assertFalse(onboarding.logged_in('claude'))


class GuideTests(unittest.TestCase):
    def test_renders_ready_nologin_and_keys(self):
        # first agent ready, the rest need login
        def fake_logged_in(tool):
            return tool == onboarding.AGENTS[0][0]

        def fake_stored_key(provider):
            return ('k', 'salva') if provider == 'groq' else ('', '')

        with patch.object(onboarding, 'resolve_bin', return_value='/bin/tool'), \
                patch.object(onboarding, 'logged_in', side_effect=fake_logged_in), \
                patch.object(onboarding.config_tool, 'stored_key', side_effect=fake_stored_key):
            out = onboarding.guide('pt')
        self.assertIn('pronto', out)
        self.assertIn('salva', out)

    def test_renders_missing_tools_and_other_language(self):
        with patch.object(onboarding, 'resolve_bin', return_value=None), \
                patch.object(onboarding.config_tool, 'stored_key', return_value=('', '')):
            out_en = onboarding.guide('en')
            out_fallback = onboarding.guide('xx')  # unknown lang -> pt strings
        self.assertIn('not installed', out_en)
        self.assertIn('install', out_en)
        self.assertIsInstance(out_fallback, str)


class MissingAndInstallTests(unittest.TestCase):
    def test_missing_lists_absent_binaries(self):
        with patch.object(onboarding, 'resolve_bin', return_value=None):
            names = onboarding.missing()
        self.assertIn('claude', names)
        with patch.object(onboarding, 'resolve_bin', return_value='/bin/x'):
            self.assertEqual(onboarding.missing(), [])

    def test_install_nothing_missing(self):
        buf = io.StringIO()
        with patch.object(onboarding, 'missing', return_value=[]), \
                patch('sys.stdout', buf):
            result = onboarding.install()
        self.assertEqual(result, [])
        self.assertIn('instalada', buf.getvalue())

    def test_install_declined_by_user(self):
        with patch('builtins.input', return_value='n'), patch('sys.stdout', io.StringIO()):
            result = onboarding.install(names=['claude'])
        self.assertEqual(result, [])

    def test_install_eof_aborts(self):
        with patch('builtins.input', side_effect=EOFError), patch('sys.stdout', io.StringIO()):
            self.assertEqual(onboarding.install(names=['claude']), [])

    def test_install_runs_and_detects_success(self):
        present = {'flag': False}

        def resolve(name):
            if name == 'npm':
                return '/bin/npm'
            return '/bin/claude' if present['flag'] else None

        def fake_call(argv):
            present['flag'] = True
            return 0

        with patch.object(onboarding, 'resolve_bin', side_effect=resolve), \
                patch.object(onboarding.subprocess, 'call', side_effect=fake_call), \
                patch('builtins.input', return_value='s'), patch('sys.stdout', io.StringIO()):
            result = onboarding.install(names=['claude'], ask=True)
        self.assertEqual(result, ['claude'])

    def test_install_reports_failure_when_binary_absent(self):
        with patch.object(onboarding, 'resolve_bin', return_value=None), \
                patch.object(onboarding.subprocess, 'call', return_value=0), \
                patch('sys.stdout', io.StringIO()) as buf:
            result = onboarding.install(names=['claude'], ask=False)
        self.assertEqual(result, [])
        self.assertIn('não foi instalado', buf.getvalue())

    def test_install_needs_npm(self):
        def resolve(name):
            return None  # npm and codex both absent

        with patch.object(onboarding, 'resolve_bin', side_effect=resolve), \
                patch.object(onboarding.subprocess, 'call', return_value=0), \
                patch('sys.stdout', io.StringIO()) as buf:
            result = onboarding.install(names=['codex'], ask=False)
        self.assertEqual(result, [])
        self.assertIn('npm', buf.getvalue())

    def test_install_manual_only_tool(self):
        # agy's install string is a URL -> install_command() is None -> manual note, nothing runnable
        with patch('sys.stdout', io.StringIO()) as buf:
            result = onboarding.install(names=['agy'], ask=False)
        self.assertEqual(result, [])
        self.assertIn('agy', buf.getvalue().lower())


class FirstRunTests(unittest.TestCase):
    def test_true_once_then_false(self):
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / 'sub' / 'onboarded'
            with patch.object(onboarding, 'MARKER', marker):
                self.assertTrue(onboarding.first_run())
                self.assertTrue(marker.exists())
                self.assertFalse(onboarding.first_run())


class MainTests(unittest.TestCase):
    def test_main_prints_guide(self):
        with patch.object(onboarding, 'guide', return_value='GUIDE') as g, \
                patch.dict('os.environ', {'LANG': 'en_US.UTF-8'}), \
                patch.object(onboarding.Path, 'read_text', side_effect=OSError), \
                patch('sys.stdout', io.StringIO()) as buf:
            onboarding.main()
        g.assert_called_once_with('en')
        self.assertIn('GUIDE', buf.getvalue())

    def test_main_install_missing(self):
        with patch.object(onboarding, 'guide', return_value='G'), \
                patch.object(onboarding, 'install') as inst, \
                patch.object(onboarding.Path, 'read_text', side_effect=OSError), \
                patch.dict('os.environ', {'LANG': 'pt_BR'}), \
                patch('sys.stdout', io.StringIO()):
            onboarding.main(install_missing=True)
        inst.assert_called_once()


if __name__ == '__main__':
    unittest.main()
