import isolation  # noqa: F401  (first: keeps the tests away from real data)
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import skills  # noqa: E402
from omni_ai.cli import base, commands  # noqa: E402
from omni_ai.chat import i18n, versions  # noqa: E402
from omni_ai.osutils import clean_terminal  # noqa: E402
from omni_ai.ui import screen  # noqa: E402

CLIPBOARD = '\x1b]52;c;ZXZpbA==\x07'
ATTACKS = [CLIPBOARD, '\x1b]0;title\x1b\\', '\x1b]52;c;x', '\x1bPq#0\x1b\\', '\x1b_apc\x1b\\', '\x1b[2K', '\x1b[1A', '\x1b[8m',
           '\x1bc', '\x1b(0', '\x9b2K', '\x9d52;c;x\x9c', '\x07', '\x08', '\r', '\x7f', '\x85']


class Clean(unittest.TestCase):
    def test_removes_every_kind_of_command(self):
        for attack in ATTACKS:
            with self.subTest(attack=attack):
                self.assertEqual(clean_terminal('a' + attack + '\nb'), 'a\nb')

    def test_keeps_ordinary_text(self):
        text = 'olá\tmundo\n**bold** [x](y) 日本語 🙂 ação ~/a_b-c.txt'
        self.assertEqual(clean_terminal(text), text)

    def test_a_sequence_split_in_two_is_harmless(self):
        joined = clean_terminal('a\x1b') + clean_terminal(']52;c;x\x07b')
        self.assertNotIn('\x1b', joined)
        self.assertNotIn('\x07', joined)

    def test_colors_only_when_asked(self):
        red = '\x1b[31mred\x1b[0m'
        self.assertEqual(clean_terminal(red), 'red')
        self.assertEqual(clean_terminal(red + CLIPBOARD + '\x1b[2K', colors=True), red)
        self.assertEqual(clean_terminal('\x9b31mred', colors=True), '\x9b31mred')


class ChatOutput(unittest.TestCase):
    def capture(self, call):
        with patch.object(screen, 'out', side_effect=lambda text='': self.lines.append(str(text))):
            call()

    def setUp(self):
        self.lines = []

    def test_view_and_replay_strip_commands(self):
        evil = 'x' + CLIPBOARD + 'y'
        view = screen.View()
        for call in (lambda: view.stream_line(evil), lambda: view.provider('codex', evil), lambda: view.action('Ran', evil),
                     lambda: view.result(evil), lambda: view.answer(evil), lambda: view.note(evil), lambda: view.error(evil),
                     lambda: screen.replay([{'role': 'user', 'content': evil}, {'role': 'assistant', 'content': evil, 'provider': evil}])):
            self.capture(call)
        text = '\n'.join(self.lines)
        self.assertNotIn(']52', text)
        self.assertNotIn('\x07', text)
        self.assertIn('xy', text)

    def test_display_strips_c1(self):
        with patch('sys.stdout', io.StringIO()) as out:
            i18n.display('a\x9d52;c;x\x9cb')
        self.assertEqual(out.getvalue(), 'ab\n')

    def test_release_text(self):
        clean = versions._clean_release({'name': 'a' + CLIPBOARD, 'draft': False})
        self.assertEqual(clean, {'name': 'a', 'draft': False})


class DirectOutput(unittest.TestCase):
    def test_tool_output_keeps_colors_and_loses_commands(self):
        output = '\x1b[31mred\x1b[0m' + CLIPBOARD + '\n'
        script = f'import sys; sys.stdout.write({output!r})'
        with patch('sys.stdout', io.StringIO()) as out:
            code, captured = base.run([sys.executable, '-c', script])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), '\x1b[31mred\x1b[0m\n')
        self.assertIn(CLIPBOARD, captured)  # limits are read from the raw text

    def test_history(self):
        conversation = {'title': 't' + CLIPBOARD, 'source': 'omni', 'updated': 0,
                        'messages': [{'role': 'assistant', 'content': 'c' + CLIPBOARD}]}
        with patch('omni_ai.history.conversation', return_value=conversation), \
                patch('sys.stdout', io.StringIO()) as out:
            commands.history_command(['abc'])
        self.assertNotIn(']52', out.getvalue())


class ProjectSkills(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home, self.project = self.root / 'home', self.root / 'project'
        self.secret = self.home / 'secret.md'
        (self.project / '.agent' / 'workflows').mkdir(parents=True)
        (self.home / '.claude' / 'commands').mkdir(parents=True)
        self.secret.write_text('sk-or-SECRET')
        patcher = patch.object(skills, 'HOME', self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_link_leaving_the_project_is_ignored(self):
        (self.project / '.agent' / 'workflows' / 'evil.md').symlink_to(self.secret)
        self.assertNotIn('evil', skills.discover(self.project))

    def test_links_inside_the_project_still_work(self):
        real = self.project / 'real.md'
        real.write_text('body')
        (self.project / '.agent' / 'workflows' / 'fine.md').symlink_to(real)
        self.assertIn('fine', skills.discover(self.project))

    def test_the_users_own_links_still_work(self):
        (self.home / '.claude' / 'commands' / 'mine.md').symlink_to(self.secret)
        self.assertEqual(skills.discover(self.project)['mine']['claude'][2], 'usuário')


if __name__ == '__main__':
    unittest.main()
