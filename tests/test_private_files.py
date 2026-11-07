"""Files that can hold conversations, code or keys stay readable only by their owner, whatever the umask."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import importlib
import json
import logging
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from omni_ai import osutils, ui
from omni_ai.cli import base
from omni_ai.config import keys
from omni_ai import server, export
from omni_ai.server import service, workspaces

spend = importlib.import_module('omni_ai.chat.spend')  # the package exports a function with this name
ROOT = Path(__file__).resolve().parent.parent


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


@unittest.skipIf(os.name == 'nt', 'POSIX permissions')
class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)


class Helper(Base):
    def test_a_new_file_is_private_and_its_new_folder_too(self):
        path = self.root / 'new' / 'file'
        with osutils.private_file(path, 'a') as stream:
            stream.write('x')
        self.assertEqual(mode(path), 0o600)
        self.assertEqual(mode(path.parent), 0o700)

    def test_a_loose_file_is_fixed_when_asked(self):
        path = self.root / 'file'
        path.write_text('old')
        path.chmod(0o644)
        with osutils.private_file(path, 'a', tighten=True) as stream:
            stream.write('new')
        self.assertEqual(mode(path), 0o600)
        self.assertEqual(path.read_text(), 'oldnew')

    def test_an_existing_folder_keeps_its_permissions(self):
        folder = self.root / 'shared'
        folder.mkdir()
        folder.chmod(0o755)
        with osutils.private_file(folder / 'file', 'w') as stream:
            stream.write('x')
        self.assertEqual(mode(folder), 0o755)

    def test_binary_and_truncate(self):
        path = self.root / 'file'
        path.write_text('long old text')
        with osutils.private_file(path, 'wb') as stream:
            stream.write(b'new')
        self.assertEqual(path.read_bytes(), b'new')


class Logs(Base):
    def test_usage_log(self):
        log = self.root / 'usage.log'
        log.write_text('')
        log.chmod(0o644)
        with patch.object(base, 'LOG_FILE', log):
            base.log_usage('codex', 'main', 'success')
        self.assertEqual(mode(log), 0o600)
        self.assertIn('tool=codex', log.read_text())

    def test_costs(self):
        costs = self.root / 'costs.jsonl'
        with patch.object(spend, 'costs_file', return_value=costs):
            spend.record_spend({'engine': 'codex', 'cost': 1, 'tokens': 2})
        self.assertEqual(mode(costs), 0o600)

    def test_server_log_and_its_rotation(self):
        path = self.root / 'serve.log'
        logger = logging.getLogger('omni_ai.server.requests')
        before = list(logger.handlers)
        self.addCleanup(lambda: setattr(logger, 'handlers', before))
        server._log_to_file(str(path))
        added = [item for item in logger.handlers if item not in before]
        self.addCleanup(lambda: [item.close() for item in added])
        logger.info('line')
        self.assertEqual(mode(path), 0o600)
        added[0].doRollover()
        logger.info('after')
        self.assertEqual(mode(path), 0o600)


class Exports(Base):
    CONV = [{'role': 'user', 'content': 'hi'}, {'role': 'assistant', 'content': 'hello'}]

    def test_markdown_export(self):
        source, out = self.root / 'conv.json', self.root / 'out.md'
        source.write_text(json.dumps(self.CONV))
        with patch.object(export, 'to_clipboard', return_value=False), patch('sys.stdout'):
            self.assertEqual(export.main(['--export', '--from', str(source), '--out', str(out)]), 0)
        self.assertEqual(mode(out), 0o600)

    def test_a_file_the_user_already_shares_keeps_its_permissions(self):
        source, out = self.root / 'conv.json', self.root / 'out.md'
        source.write_text(json.dumps(self.CONV))
        out.write_text('old')
        out.chmod(0o644)
        with patch.object(export, 'to_clipboard', return_value=False), patch('sys.stdout'):
            export.main(['--export', '--from', str(source), '--out', str(out)])
        self.assertEqual(mode(out), 0o644)

    def test_pdf_export(self):
        out = self.root / 'out.pdf'
        source = self.root / 'conv.json'
        source.write_text(json.dumps(self.CONV))
        with patch('sys.stdout'):
            self.assertEqual(export.main(['--print', '--from', str(source), '--out', str(out)]), 0)
        self.assertEqual(mode(out), 0o600)

    def test_no_export_opens_a_file_without_the_helper(self):
        for name in ('omni_ai/export/__init__.py', 'omni_ai/export/pdf.py', 'omni_ai/chat/__init__.py', 'omni_ai/pii/__init__.py'):
            with self.subTest(name=name):
                source = (ROOT / name).read_text(encoding='utf-8')
                self.assertNotRegex(source, r"open\((out_path|out|args\.map_out), ['\"]w")

    def test_pii_map(self):
        out = self.root / 'map.json'
        code = subprocess.run([sys.executable, '-P', '-m', 'omni_ai.pii', 'mask', '--map-out', str(out)], input='me@example.com',
                              capture_output=True, text=True, env={**os.environ, 'PYTHONPATH': str(ROOT)})
        self.assertEqual(code.returncode, 0, code.stderr)
        self.assertEqual(mode(out), 0o600)


class Work(Base):
    def test_the_folder_of_requests_is_private(self):
        folder = self.root / 'project'
        folder.mkdir()
        (folder / 'a').write_text('x')
        with patch.dict(os.environ, {'HOME': str(self.root)}), patch.object(Path, 'home', return_value=self.root):
            spaces = workspaces.Workspaces(folder)
            work = spaces.acquire({})
            self.addCleanup(work.finish)
            self.assertEqual(mode(spaces.root), 0o700)

    def test_an_old_loose_folder_is_fixed(self):
        folder = self.root / 'project'
        folder.mkdir()
        (folder / 'a').write_text('x')
        with patch.dict(os.environ, {'HOME': str(self.root)}), patch.object(Path, 'home', return_value=self.root):
            root = workspaces.runs_root(folder.resolve())
            root.mkdir(parents=True)
            root.chmod(0o755)
            spaces = workspaces.Workspaces(folder)
            work = spaces.acquire({})
            self.addCleanup(work.finish)
            self.assertEqual(mode(spaces.root), 0o700)


class Keys(Base):
    def test_a_loose_key_file_is_fixed_when_read(self):
        folder = self.root / 'keys'
        folder.mkdir()
        path = folder / 'groq'
        path.write_text('gsk_value')
        path.chmod(0o644)
        with patch.object(keys, 'KEYS_DIR', folder), patch.object(keys, 'key_path', return_value=path):
            self.assertEqual(keys.stored_keys('groq')[0][1], 'gsk_value')
        self.assertEqual(mode(path), 0o600)

    def test_every_known_key_stays_out_of_the_history(self):
        for text in ('xai-abcdefghijklmnopqrstuvwx', 'gsk_abcdefghijklmnopqrstuvwx', 'csk-abcdefghijklmnopqrstuvwx',
                     'sk-or-abcdefghijklmnopqrstuvwx', 'my key is xai-abcdefghijklmnopqrstuvwx ok'):
            with self.subTest(text=text):
                self.assertTrue(ui.SECRET.search(text))

    def test_normal_text_stays_in_the_history(self):
        for text in ('explain the xai-sdk package', 'how do I use gsk_ prefixes?', 'fix the bug'):
            with self.subTest(text=text):
                self.assertFalse(ui.SECRET.search(text))


class ServiceLog(Base):
    def test_the_service_never_generates_a_key_into_its_log(self):
        with patch.object(server.auth, 'configured', return_value=False), patch.object(server.auth, 'add') as add, \
                patch.object(server, 'Server', side_effect=OSError('not in this test')), \
                patch.dict(os.environ, {'SMART_AI_SERVICE': '1'}), patch('sys.stderr'), patch('sys.stdout'):
            code = server.main(['--port', '0'])
        add.assert_not_called()
        self.assertEqual(code, service.FATAL_EXIT)

    def test_the_unit_says_it_is_a_service(self):
        self.assertEqual(service.environment({})['SMART_AI_SERVICE'], '1')


if __name__ == '__main__':
    unittest.main()
