import isolation  # noqa: F401  (first: keeps the tests away from real data)
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)
sys.path.insert(0, str(ROOT))
from omni_ai import export


SESSION = {
    'id': 'abc123def456', 'title': 'Bug no auth', 'cwd': '/proj', 'created': 1700000000,
    'messages': [
        {'role': 'user', 'content': 'ajuda no token expiry ' + 'palavra ' * 80},
        {'role': 'assistant', 'provider': 'claude',
         'content': 'use <= no check\n```python\nif a <= b:\n    pass\n```\nfim'},
        {'role': 'changes', 'content': 'auth.py'},
        {'role': 'cli', 'content': 'saída do comando', 'provider': '/usage'},
    ],
}


class NormalizeTests(unittest.TestCase):
    def test_accepts_session_dict(self):
        conv = export.normalize(SESSION)
        self.assertEqual(conv['id'], 'abc123def456')
        self.assertEqual(len(conv['messages']), 4)
        self.assertEqual(conv['messages'][1]['provider'], 'claude')

    def test_accepts_list_and_json_string(self):
        conv = export.normalize([{'role': 'user', 'content': 'oi'}])
        self.assertEqual(conv['messages'][0]['content'], 'oi')
        self.assertEqual(export.normalize(json.dumps(SESSION))['title'], 'Bug no auth')

    def test_title_falls_back_to_first_message(self):
        conv = export.normalize({'messages': [{'role': 'user', 'content': 'primeira linha\nsegunda'}]})
        self.assertEqual(conv['title'], 'primeira linha')

    def test_title_default_when_empty(self):
        self.assertEqual(export.normalize({'messages': []})['title'], 'Conversa')

    def test_non_string_and_none_content_coerced(self):
        conv = export.normalize({'messages': [
            {'role': 'user', 'content': None},
            {'role': 'assistant', 'content': {'a': 1}},
            {'role': 'assistant', 'agent': 'agente-1', 'content': 'x'},
        ]})
        self.assertEqual(conv['messages'][0]['content'], '')
        self.assertIn('"a": 1', conv['messages'][1]['content'])
        self.assertEqual(conv['messages'][2]['provider'], 'agente-1')


class HelpersTests(unittest.TestCase):
    def test_when_valid_and_invalid(self):
        self.assertTrue(export._when(1700000000).startswith('2023-'))
        self.assertRegex(export._when('nope'), r'^\d{4}-')
        self.assertRegex(export._when(None), r'^\d{4}-')

    def test_default_name_slug(self):
        name = export.default_name({'title': 'Mundo! #1'}, 'pdf')
        self.assertTrue(name.startswith('omni-mundo-1-'))
        self.assertTrue(name.endswith('.pdf'))

    def test_default_name_empty_title(self):
        self.assertTrue(export.default_name({'title': '***'}, 'md').startswith('omni-conversa-'))

    def test_role_label(self):
        self.assertEqual(export._role_label('user'), 'Você')
        self.assertEqual(export._role_label('weird'), 'Weird')

    def test_esc(self):
        self.assertEqual(export._esc('a(b)c\\d'), r'a\(b\)c\\d')

    def test_wrap_short_and_long_and_giant_word(self):
        self.assertEqual(export._wrap('curto', False, 504), ['curto'])
        many = export._wrap('palavra ' * 100, False, 504)
        self.assertTrue(len(many) > 1)
        giant = export._wrap('x' * 400, True, 504)
        self.assertTrue(all(len(line) <= 84 for line in giant))
        self.assertEqual(export._wrap('', False, 504), [''])


class MarkdownTests(unittest.TestCase):
    def test_contains_sections_and_model(self):
        conv = export.normalize(SESSION)
        text = export.to_markdown(conv, 'gpt')
        self.assertIn('# Conversa Omni AI — Bug no auth', text)
        self.assertIn('## Instrução de continuidade', text)
        self.assertIn('na gpt', text)
        self.assertIn('## Histórico', text)
        self.assertIn('**Você:**', text)
        self.assertIn('**Assistente (claude):**', text)
        self.assertNotIn('Arquivos alterados', text)  # changes são omitidos no copy

    def test_no_model_generic_phrase(self):
        text = export.to_markdown(export.normalize(SESSION))
        self.assertIn('em outro assistente', text)

    def test_handles_missing_meta(self):
        text = export.to_markdown(export.normalize({'messages': [{'role': 'user', 'content': 'oi'}]}))
        self.assertIn('Continue a conversa a partir daqui.', text)


class PdfTests(unittest.TestCase):
    def _pdf(self, conv):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'c.pdf')
            export.to_pdf(conv, out)
            return Path(out).read_bytes()

    def test_structure(self):
        data = self._pdf(export.normalize(SESSION))
        self.assertTrue(data.startswith(b'%PDF-1.4'))
        self.assertTrue(data.rstrip().endswith(b'%%EOF'))
        for token in (b'/Type /Catalog', b'/Type /Pages', b'/Type /Page ',
                      b'/BaseFont /Helvetica', b'/WinAnsiEncoding', b'startxref', b'xref'):
            self.assertIn(token, data)

    def test_multipage(self):
        big = {'messages': [{'role': 'user', 'content': 'linha ' * 30}] * 200}
        data = self._pdf(export.normalize(big))
        self.assertGreater(data.count(b'/Type /Page '), 1)

    def test_empty_conversation_still_valid(self):
        data = self._pdf(export.normalize({'messages': []}))
        self.assertIn(b'/Type /Page ', data)

    def test_accents_encoded_cp1252(self):
        data = self._pdf(export.normalize({'messages': [{'role': 'user', 'content': 'ação coração'}]}))
        self.assertIn('ção'.encode('cp1252'), data)


class ClipboardTests(unittest.TestCase):
    def test_copies_with_first_available_tool(self):
        with patch('shutil.which', side_effect=lambda c: '/usr/bin/' + c if c == 'pbcopy' else None), \
             patch('subprocess.run') as run:
            self.assertTrue(export.to_clipboard('oi'))
            run.assert_called_once()

    def test_returns_false_when_nothing_available(self):
        with patch('shutil.which', return_value=None):
            self.assertFalse(export.to_clipboard('oi'))

    def test_swallows_tool_failure(self):
        import subprocess
        with patch('shutil.which', return_value='/usr/bin/pbcopy'), \
             patch('subprocess.run', side_effect=OSError):
            self.assertFalse(export.to_clipboard('oi'))


class MainTests(unittest.TestCase):
    def _run(self, argv, conv=SESSION):
        """Runs main() with cwd set to a temp dir; returns (code, printed_path, exists, body)."""
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(d, ignore_errors=True))
        src = os.path.join(d, 'conv.json')
        Path(src).write_text(json.dumps(conv))
        cwd = os.getcwd()
        os.chdir(d)
        try:
            buf, err = io.StringIO(), io.StringIO()
            with patch('sys.stdout', buf), patch('sys.stderr', err), \
                 patch.object(export, 'to_clipboard', return_value=False):
                code = export.main(argv + ['--from', src])
            path = buf.getvalue().strip()
            exists = os.path.exists(path)
            body = Path(path).read_text() if (exists and path.endswith('.md')) else ''
            return code, path, exists, body
        finally:
            os.chdir(cwd)

    def test_print_writes_pdf(self):
        code, path, exists, _ = self._run(['--print'])
        self.assertEqual(code, 0)
        self.assertTrue(path.endswith('.pdf'))
        self.assertTrue(exists)

    def test_export_writes_md_with_model(self):
        code, path, exists, body = self._run(['--export', 'gemini'])
        self.assertEqual(code, 0)
        self.assertTrue(path.endswith('.md'))
        self.assertTrue(exists)
        self.assertIn('na gemini', body)

    def test_export_clipboard_note(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'c.json')
            Path(src).write_text(json.dumps(SESSION))
            out = os.path.join(d, 'o.md')
            buf, err = io.StringIO(), io.StringIO()
            with patch('sys.stdout', buf), patch('sys.stderr', err), \
                 patch.object(export, 'to_clipboard', return_value=True):
                export.main(['--export', '--from', src, '--out', out])
            self.assertIn('transfer', err.getvalue())

    def test_out_flag_respected(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'c.json')
            Path(src).write_text(json.dumps(SESSION))
            out = os.path.join(d, 'custom.pdf')
            with patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
                export.main(['--print', '--from', src, '--out', out])
            self.assertTrue(os.path.exists(out))

    def test_stdin_when_no_from(self):
        with patch('sys.stdin', io.StringIO(json.dumps(SESSION))), \
             patch('sys.stdout', io.StringIO()) as out, patch('sys.stderr', io.StringIO()), \
             patch.object(export, 'to_pdf') as pdf:
            code = export.main(['--print'])
        self.assertEqual(code, 0)
        pdf.assert_called_once()

    def test_bad_json_returns_1(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'c.json')
            Path(src).write_text('not json')
            with patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()) as err:
                code = export.main(['--print', '--from', src])
        self.assertEqual(code, 1)
        self.assertIn('Não foi possível ler', err.getvalue())

    def test_unknown_mode_returns_2(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'c.json')
            Path(src).write_text(json.dumps(SESSION))
            with patch('sys.stdout', io.StringIO()), patch('sys.stderr', io.StringIO()):
                code = export.main(['--bogus', '--from', src])
        self.assertEqual(code, 2)


if __name__ == '__main__':
    unittest.main()
