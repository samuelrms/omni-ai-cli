import isolation  # noqa: F401  (first: keeps the tests away from real data)
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)
sys.path.insert(0, str(ROOT))
from omni_ai import ui


class FakeTTY(io.StringIO):
    """StringIO that also reports as a terminal."""
    def isatty(self):
        return True


@contextlib.contextmanager
def color_on(truecolor=True):
    with patch.object(ui.color, 'COLOR', True), patch.object(ui.color, 'TRUECOLOR', truecolor):
        yield


class ColorPrimitiveTests(unittest.TestCase):
    def test_c_identity_without_color(self):
        with patch.object(ui.color, 'COLOR', False):
            self.assertEqual(ui._c('1')('hi'), 'hi')

    def test_c_wraps_with_color(self):
        with color_on():
            self.assertEqual(ui._c('1')('hi'), '\033[1mhi\033[0m')

    def test_rgb_plain_without_color(self):
        with patch.object(ui.color, 'COLOR', False):
            self.assertEqual(ui.rgb((1, 2, 3), 'x'), 'x')

    def test_rgb_truecolor_foreground_and_background(self):
        with color_on(truecolor=True):
            self.assertIn('38;2;10;20;30', ui.rgb((10, 20, 30), 'x'))
            self.assertIn('48;2;10;20;30', ui.rgb((10, 20, 30), 'x', background=True))

    def test_rgb_256_cube(self):
        with color_on(truecolor=False):
            out = ui.rgb((255, 0, 0), 'x')
            self.assertIn('38;5;', out)

    def test_mix_clamps_and_interpolates(self):
        stops = [(0, 0, 0), (10, 10, 10)]
        self.assertEqual(ui._mix(stops, -1), (0, 0, 0))
        self.assertEqual(ui._mix(stops, 2), (10, 10, 10))
        self.assertEqual(ui._mix(stops, 0.5), (5, 5, 5))

    def test_hue_returns_rgb_triplet(self):
        r, g, b = ui.hue(0.3)
        self.assertTrue(all(0 <= v <= 255 for v in (r, g, b)))

    def test_rainbow_colors_each_char(self):
        with color_on():
            self.assertIn('\033[', ui.rainbow('abc', 0.5))

    def test_gradient_plain_and_colored(self):
        with patch.object(ui.color, 'COLOR', False):
            self.assertEqual(ui.gradient('abc'), 'abc')
        with color_on():
            self.assertIn('\033[', ui.gradient('abc'))

    def test_badge_plain_and_colored(self):
        with patch.object(ui.color, 'COLOR', False):
            self.assertEqual(ui.badge('ERRO', (1, 2, 3)), '[ERRO]')
        with color_on():
            self.assertIn('ERRO', ui.badge('ERRO', (1, 2, 3)))

    def test_shimmer_plain_and_colored(self):
        with patch.object(ui.color, 'COLOR', False):
            self.assertEqual(ui.shimmer('hello', 3), 'hello')
        with color_on():
            self.assertIn('\033[', ui.shimmer('hello world', 3))


class TextHelperTests(unittest.TestCase):
    def test_t_uses_language_and_falls_back_to_pt(self):
        with patch.object(ui.text, 'LANG', ['en']):
            self.assertEqual(ui.t('cancel'), 'Ctrl-C cancels')
        with patch.object(ui.text, 'LANG', ['xx']):  # unknown -> pt
            self.assertEqual(ui.t('cancel'), 'Ctrl-C cancela')

    def test_t_formats_values(self):
        with patch.object(ui.text, 'LANG', ['pt']):
            self.assertIn('5', ui.t('more', n=5))

    def test_width_clamped_to_minimum(self):
        with patch('omni_ai.ui.text.shutil.get_terminal_size', return_value=os.terminal_size((10, 30))):
            self.assertEqual(ui.width(), 40)
        with patch('omni_ai.ui.text.shutil.get_terminal_size', return_value=os.terminal_size((120, 30))):
            self.assertEqual(ui.width(), 120)

    def test_short_truncates_and_collapses_whitespace(self):
        self.assertEqual(ui.short('a    b', 10), 'a b')
        self.assertTrue(ui.short('abcdefghij', 5).endswith('…'))

    def test_home_path(self):
        home = str(Path.home())
        self.assertEqual(ui.home_path(home + '/projects'), '~/projects')
        self.assertEqual(ui.home_path('/etc/hosts'), '/etc/hosts')

    def test_elapsed_seconds_and_minutes(self):
        import time
        with patch('omni_ai.ui.screen.time.time', return_value=1000.0):
            self.assertEqual(ui.elapsed(995.0), '5s')
            self.assertEqual(ui.elapsed(900.0), '1m 40s')

    def test_escape(self):
        self.assertEqual(ui.escape('<a & b>'), '&lt;a &amp; b&gt;')

    def test_fuzzy_ranks(self):
        self.assertEqual(ui.fuzzy('ab', 'abc'), 0)
        self.assertEqual(ui.fuzzy('bc', 'abc'), 1)
        self.assertEqual(ui.fuzzy('ac', 'abc'), 2)
        self.assertIsNone(ui.fuzzy('zz', 'abc'))


class MarkdownTests(unittest.TestCase):
    def test_headings_bullets_bold_code_and_fence(self):
        text = '# Title\n- item one\n* item two\n**strong** and `code`\n```\nfenced line\n```\nplain'
        out = ui.markdown(text)
        self.assertIn('Title', out)
        self.assertIn('• item one', out)
        self.assertIn('• item two', out)
        self.assertIn('strong', out)
        self.assertIn('code', out)
        self.assertIn('fenced line', out)
        self.assertIn('plain', out)


class ViewTests(unittest.TestCase):
    def capture(self, fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_stream_line_first_then_continuation(self):
        view = ui.View()
        text = self.capture(lambda: (view.stream_line('first'), view.stream_line('second')))
        self.assertIn('first', text)
        self.assertIn('second', text)

    def test_provider_with_and_without_detail(self):
        view = ui.View()
        self.assertIn('codex', self.capture(lambda: view.provider('codex', 'gpt')))
        self.assertIn('codex', self.capture(lambda: view.provider('codex', '')))

    def test_action_with_and_without_target(self):
        view = ui.View()
        with patch('omni_ai.ui.text.width', return_value=100):
            self.assertIn('Reading', self.capture(lambda: view.action('Reading', 'file.py')))
            self.assertIn('Done', self.capture(lambda: view.action('Done', '')))

    def test_result_empty_single_and_multiline(self):
        view = ui.View()
        with patch('omni_ai.ui.text.width', return_value=100):
            self.assertIn('sem saída', self.capture(lambda: view.result('   ')))
            self.assertIn('only', self.capture(lambda: view.result('only line')))
            multi = self.capture(lambda: view.result('a\nb\nc', failed=True))
        self.assertIn('linhas', multi)

    def test_answer_renders_markdown_body(self):
        view = ui.View()
        text = self.capture(lambda: view.answer('# Heading\nbody line'))
        self.assertIn('Heading', text)
        self.assertIn('body line', text)

    def test_note_limit_warn_and_plain(self):
        view = ui.View()
        with patch.object(ui.text, 'LANG', ['pt']):
            self.assertIn('LIMITE', self.capture(lambda: view.note('rate limit reached, try again')))
            self.assertIn('AVISO', self.capture(lambda: view.note('⚠ something off')))
            self.assertIn('just a note', self.capture(lambda: view.note('just a note')))

    def test_error_dedup_limit_and_plain(self):
        view = ui.View()
        with patch('omni_ai.ui.text.width', return_value=100), patch.object(ui.text, 'LANG', ['pt']):
            first = self.capture(lambda: view.error('boom failure'))
            self.assertIn('ERRO', first)
            # same error again is suppressed
            self.assertEqual(self.capture(lambda: view.error('boom failure')), '')
            self.assertIn('LIMITE', self.capture(lambda: view.error('quota exceeded')))

    def test_prefix_applied(self):
        view = ui.View(prefix='[a] ')
        text = self.capture(lambda: view.provider('codex', ''))
        self.assertIn('[a] ', text)


class ReplayTests(unittest.TestCase):
    def capture(self, messages, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ui.replay(messages, **kw)
        return buf.getvalue()

    def test_user_and_assistant_with_provider_note(self):
        messages = [
            {'role': 'user', 'content': 'line1\nline2'},
            {'role': 'assistant', 'content': 'answer body', 'provider': 'via codex'},
        ]
        text = self.capture(messages)
        self.assertIn('line1', text)
        self.assertIn('line2', text)
        self.assertIn('answer body', text)
        self.assertIn('codex', text)

    def test_older_messages_truncation_banner(self):
        messages = [{'role': 'user', 'content': f'm{i}'} for i in range(50)]
        with patch.object(ui.text, 'LANG', ['pt']):
            text = self.capture(messages, limit=10)
        self.assertIn('anteriores', text)


class TerminalControlTests(unittest.TestCase):
    def test_attention_rings_bell_and_sets_title_when_tty(self):
        fake = FakeTTY()
        with patch.object(sys, 'stdout', fake):
            ui.attention('Done')
        self.assertIn('\a', fake.getvalue())
        self.assertIn('Done', fake.getvalue())

    def test_attention_noop_when_not_tty(self):
        buf = io.StringIO()
        with patch.object(sys, 'stdout', buf):
            ui.attention('Done')
        self.assertEqual(buf.getvalue(), '')

    def test_set_title_strips_control_chars(self):
        fake = FakeTTY()
        with patch.object(sys, 'stdout', fake):
            ui.set_title('a\x01b')
        self.assertIn('\033]0;', fake.getvalue())
        self.assertNotIn('\x01', fake.getvalue())

    def test_clear_screen_when_tty(self):
        fake = FakeTTY()
        with patch.object(sys, 'stdout', fake):
            ui.clear_screen()
        self.assertIn('\033[2J', fake.getvalue())

    def test_out_prints_and_clears_spinner(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ui.out('hello')
        self.assertIn('hello', buf.getvalue())


class OrbitTests(unittest.TestCase):
    def test_orbit_step_zero_and_nonzero(self):
        with color_on():
            zero = ui.orbit(0)
            moved = ui.orbit(3)
        self.assertEqual(len(zero), 3)
        self.assertEqual(len(moved), 3)


class SpinnerTests(unittest.TestCase):
    def test_start_returns_early_when_embedded(self):
        sp = ui.Spinner()
        sp.embedded = True
        sp.start('working')
        self.assertIsNone(sp.thread)

    def test_start_returns_early_when_not_tty(self):
        sp = ui.Spinner()
        with patch.object(sys, 'stdout', io.StringIO()):
            sp.start('working')
        self.assertIsNone(sp.thread)

    def test_frame_returns_a_frame_char(self):
        sp = ui.Spinner()
        self.assertIn(sp.frame(), ui.Spinner.FRAMES)

    def test_clear_when_visible(self):
        sp = ui.Spinner()
        sp.visible = True
        buf = io.StringIO()
        with patch.object(sys, 'stdout', buf):
            sp.clear()
        self.assertIn('\r', buf.getvalue())
        self.assertFalse(sp.visible)

    def test_start_and_stop_thread_runs_loop(self):
        sp = ui.Spinner()
        fake = FakeTTY()
        with color_on(), patch.object(sys, 'stdout', fake), patch.object(ui.text, 'LANG', ['pt']):
            sp.start('loading')
            # let the loop tick at least once
            import time as _t
            deadline = _t.time() + 2
            while not sp.visible and _t.time() < deadline:
                _t.sleep(0.02)
            sp.stop()
        self.assertIsNone(sp.thread)


class SplashTests(unittest.TestCase):
    def test_splash_skipped_without_color(self):
        with patch.object(ui.color, 'COLOR', False):
            self.assertFalse(ui.splash())

    def test_splash_skipped_by_env(self):
        fake = FakeTTY()
        with color_on(), patch.object(sys, 'stdout', fake), \
             patch.dict(os.environ, {'SMART_AI_NO_SPLASH': '1'}):
            self.assertFalse(ui.splash())

    def test_splash_big_layout_runs(self):
        fake = FakeTTY()
        with color_on(), patch.object(sys, 'stdout', fake), \
             patch.dict(os.environ, {}, clear=False), \
             patch('omni_ai.ui.screen.time.sleep'), \
             patch('omni_ai.ui.screen.shutil.get_terminal_size', return_value=os.terminal_size((120, 40))):
            os.environ.pop('SMART_AI_NO_SPLASH', None)
            self.assertTrue(ui.splash(seconds=0.1))
        self.assertIn('\033[?1049h', fake.getvalue())

    def test_splash_small_layout_runs(self):
        fake = FakeTTY()
        with color_on(), patch.object(sys, 'stdout', fake), \
             patch('omni_ai.ui.screen.time.sleep'), \
             patch('omni_ai.ui.screen.shutil.get_terminal_size', return_value=os.terminal_size((50, 20))):
            os.environ.pop('SMART_AI_NO_SPLASH', None)
            self.assertTrue(ui.splash(seconds=0.1))


class WelcomeTests(unittest.TestCase):
    def test_welcome_static_without_animation(self):
        buf = io.StringIO()
        with patch.object(ui.color, 'COLOR', False), contextlib.redirect_stdout(buf), \
             patch('omni_ai.ui.text.width', return_value=80), patch.object(ui.text, 'LANG', ['pt']):
            ui.welcome('sess-1', '/home/u/proj', 'codex', 'gpt', 'default', 'pt-BR', animate=False)
        self.assertIn('sess-1', buf.getvalue())
        self.assertIn('Omni AI', buf.getvalue())

    def test_welcome_mode_chip_shown(self):
        buf = io.StringIO()
        with patch.object(ui.color, 'COLOR', False), contextlib.redirect_stdout(buf), \
             patch('omni_ai.ui.text.width', return_value=80), patch.object(ui.text, 'LANG', ['pt']):
            ui.welcome('s', '/p', 'codex', 'gpt', 'yolo', 'pt', animate=False)
        self.assertIn('yolo', buf.getvalue())

    def test_welcome_animated_runs(self):
        fake = FakeTTY()
        with color_on(), patch.object(sys, 'stdout', fake), \
             patch('omni_ai.ui.screen.time.sleep'), patch('omni_ai.ui.text.width', return_value=80), \
             patch.object(ui.text, 'LANG', ['pt']):
            ui.welcome('s', '/home/u/p', 'codex', 'gpt', 'plan', 'pt', animate=True)
        # the title characters are individually colored, so check for the box frame + a redraw sequence
        self.assertIn('╭', fake.getvalue())
        self.assertIn('\033[2K', fake.getvalue())


class ProjectFilesTests(unittest.TestCase):
    def test_uses_git_listing_when_available(self):
        result = MagicMock(returncode=0, stdout='a.py\nb.py\n')
        with patch('subprocess.run', return_value=result):
            files = ui.project_files('/repo')
        self.assertEqual(files, ['a.py', 'b.py'])

    def test_falls_back_to_walk_when_not_a_repo(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'src').mkdir()
            (root / 'src' / 'app.py').write_text('x')
            (root / 'node_modules').mkdir()
            (root / 'node_modules' / 'lib.js').write_text('x')
            result = MagicMock(returncode=1, stdout='')
            with patch('subprocess.run', return_value=result):
                files = ui.project_files(root)
        self.assertIn(os.path.join('src', 'app.py'), files)
        self.assertFalse(any('node_modules' in f for f in files))

    def test_walk_respects_limit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i in range(10):
                (root / f'f{i}.txt').write_text('x')
            with patch('subprocess.run', side_effect=OSError()):
                files = ui.project_files(root, limit=3)
        self.assertEqual(len(files), 3)


class InputBoxTests(unittest.TestCase):
    def test_non_tty_has_no_session_and_ask_uses_input(self):
        with patch.object(sys.stdin, 'isatty', return_value=False, create=True):
            box = ui.InputBox('/tmp/hist', lambda: [], lambda: '', '', '')
        self.assertIsNone(box.session)
        with patch('builtins.input', return_value='typed'):
            self.assertEqual(box.ask(), 'typed')

    @contextlib.contextmanager
    def _app_session(self):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            yield

    def _tty_box(self, tmp, **kw):
        hist = Path(tmp) / 'history'
        defaults = dict(
            history_file=str(hist),
            commands=lambda: [('/clear', 'limpa'), ('/help', 'ajuda')],
            status=lambda: 'Omni AI · ready',
            placeholder=lambda: 'type here',
            hints='hints',
            arguments=lambda cmd, done: [('opt1', 'meta1'), ('opt2', 'meta2')],
            cwd=lambda: tmp,
            mode=lambda: 'plan',
            activity=lambda: ['running task'],
        )
        defaults.update(kw)
        with patch.object(sys.stdin, 'isatty', return_value=True), \
             patch.object(sys.stdout, 'isatty', return_value=True), \
             self._app_session():
            return ui.InputBox(**defaults)

    def test_tty_builds_session_and_completers(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = self._tty_box(tmp)
            self.assertIsNotNone(box.session)
            from prompt_toolkit.document import Document
            completer = box.session.completer
            # command completion
            cmds = list(completer.get_completions(Document('/cl'), None))
            self.assertTrue(any('clear' in c.text for c in cmds))
            # @file mention completion
            (Path(tmp) / 'file.py').write_text('x')
            with patch('omni_ai.ui.inputbox.project_files', return_value=['file.py']):
                mentions = list(completer.get_completions(Document('@fi'), None))
            self.assertTrue(any('file.py' in c.text for c in mentions))
            # argument completion (after a command + space)
            args = list(completer.get_completions(Document('/clear '), None))
            self.assertTrue(any('opt' in c.text for c in args))
            # plain text (no slash) yields nothing
            self.assertEqual(list(completer.get_completions(Document('hello'), None)), [])

    def test_history_rejects_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = self._tty_box(tmp)
            history = box.session.history
            history.store_string('normal line')
            history.store_string('/key sk-ABCDEFGHIJKLMNOPQRSTUVWX')
            stored = history.load_history_strings()
            self.assertIn('normal line', stored)
            self.assertFalse(any('/key' in s for s in stored))

    def test_ask_renders_message_and_toolbar(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = self._tty_box(tmp)
            fake = FakeTTY()
            with patch.object(box.session, 'prompt', return_value='answer') as prompt, \
                 patch.object(sys, 'stdout', fake), \
                 patch('omni_ai.ui.text.width', return_value=80), patch.object(ui.text, 'LANG', ['pt']):
                result = box.ask()
            self.assertEqual(result, 'answer')
            # exercise the message and toolbar callables passed to prompt
            msg = prompt.call_args.args[0]
            self.assertIsNotNone(msg())
            toolbar = prompt.call_args.kwargs['bottom_toolbar']
            self.assertIsNotNone(toolbar())

    def test_ask_toolbar_default_mode_no_chip(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = self._tty_box(tmp, mode=None, status=lambda: 'ready', hints=lambda: 'h', activity=None)
            fake = FakeTTY()
            with patch.object(box.session, 'prompt', return_value='x') as prompt, \
                 patch.object(sys, 'stdout', fake), patch('omni_ai.ui.text.width', return_value=80):
                box.ask()
            toolbar = prompt.call_args.kwargs['bottom_toolbar']
            self.assertIsNotNone(toolbar())
            msg = prompt.call_args.args[0]
            self.assertIsNotNone(msg())

    def test_ask_args_prompts_for_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = self._tty_box(tmp)
            with patch.object(box.session, 'prompt', return_value='chosen') as prompt, \
                 patch.object(ui.text, 'LANG', ['pt']):
                result = box.ask_args('/skill', 'hint text', ['a', 'b'])
            self.assertEqual(result, 'chosen')
            prompt.assert_called_once()


if __name__ == '__main__':
    unittest.main()
