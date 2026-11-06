import isolation  # noqa: F401
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from omni_ai import cli


class PromptFile(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / 'request.txt'

    def routed(self, *argv):
        with patch.object(cli, 'route', return_value=0) as route:
            self.assertEqual(cli.main(list(argv)), 0)
        return route.call_args.args

    def test_the_text_is_the_request_as_written(self):
        text = 'it\'s "x" & y %PATH% --codex ação\nsecond line'
        self.file.write_text(text, encoding='utf-8')
        self.assertEqual(self.routed('--prompt-file', str(self.file)), (text, 'auto'))

    def test_flags_before_it_still_count(self):
        self.file.write_text('hello', encoding='utf-8')
        self.assertEqual(self.routed('--codex', '--prompt-file', str(self.file)), ('hello', 'codex'))

    def test_after_the_separator_it_is_only_text(self):
        self.assertEqual(self.routed('--', '--prompt-file', str(self.file)), (f'--prompt-file {self.file}', 'auto'))

    def test_missing_or_unreadable_file_is_an_error(self):
        self.file.write_bytes(b'\xff\xfe')
        for argv in (['--prompt-file'], ['--prompt-file', str(self.file) + '.gone'], ['--prompt-file', str(self.file)]):
            with self.subTest(argv=argv), patch.object(cli, 'route') as route, patch('sys.stderr', io.StringIO()) as err:
                self.assertEqual(cli.main(argv), 1)
                route.assert_not_called()
                self.assertIn('arquivo do pedido', err.getvalue())

    def test_it_is_offered_in_completion(self):
        self.assertIn('--prompt-file', cli.completion_words())


if __name__ == '__main__':
    unittest.main()
