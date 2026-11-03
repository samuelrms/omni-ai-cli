import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import chat, cli  # noqa: E402


class VersionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for target in (patch.dict(os.environ, {'SMART_AI_LOG': str(self.root / 'usage.log')}),
                       patch.object(cli, 'free_classifiers', return_value=[('groq', 'https://example.invalid', {})]),
                       patch.object(chat.config_tool, 'stored_keys', return_value=[('principal', 'gsk_x', 'file')])):
            target.start()
            self.addCleanup(target.stop)
        self.asked = []

    def reply(self, text):
        def ask(url, key, data, timeout=6):
            self.asked.append(data['messages'][1]['content'])
            return text
        return patch.object(cli, 'ask_text', side_effect=ask)

    def test_translates_and_caches(self):
        with self.reply('1. Mostra o custo\n2. Fila editável'):
            self.assertEqual(chat.translate(['show the cost', 'edit the queue'], 'pt'), ['Mostra o custo', 'Fila editável'])
        with self.reply('1. não deveria ser usado'):
            self.assertEqual(chat.translate(['show the cost'], 'pt'), ['Mostra o custo'])
        self.assertEqual(len(self.asked), 1)  # second call came from the cache

    def test_english_and_bad_replies_keep_the_original(self):
        with self.reply('1. só uma linha'):
            self.assertEqual(chat.translate(['a', 'b'], 'pt'), ['a', 'b'])  # count mismatch
            self.assertEqual(chat.translate(['a'], 'en'), ['a'])
        with self.reply(None):
            self.assertEqual(chat.translate(['a'], 'fr'), ['a'])

    def release(self, tag):
        return [{'tag_name': tag, 'draft': False, 'prerelease': False, 'body': '* **chat:** edit the queue ([abc1234](x))'}]

    def test_newer_release_only_for_real_versions(self):
        with patch.object(chat, 'github', return_value=self.release('v1.3.0')) as fetch:
            with patch.object(chat, 'installed_version', return_value='0.0.0-development'):
                self.assertIsNone(chat.newer_release())
            with patch.object(chat, 'installed_version', return_value='1.2.9'):
                self.assertEqual(chat.newer_release()['tag_name'], 'v1.3.0')
                self.assertEqual(chat.newer_release()['tag_name'], 'v1.3.0')
            with patch.object(chat, 'installed_version', return_value='1.3.0'):
                self.assertIsNone(chat.newer_release())
        self.assertEqual(fetch.call_count, 1)  # checked once a day

    def test_check_again_after_a_day(self):
        stamp = self.root / 'release_check.json'
        stamp.write_text(json.dumps({'at': time.time() - 90000, 'release': None}))
        with patch.object(chat, 'github', return_value=self.release('v2.0.0')), \
                patch.object(chat, 'installed_version', return_value='1.0.0'):
            self.assertEqual(chat.newer_release()['tag_name'], 'v2.0.0')

    def test_offline_is_silent(self):
        with patch.object(chat, 'github', side_effect=OSError), patch.object(chat, 'installed_version', return_value='1.0.0'):
            self.assertIsNone(chat.newer_release())

    def test_announcement_line(self):
        chat.set_lang('pt')
        lines = []
        with patch.object(chat, 'newer_release', return_value=self.release('v1.3.0')[0]), \
                patch.object(chat.ui, 'out', side_effect=lines.append), self.reply('1. Edita a fila'):
            chat.announce_release()
            deadline = time.time() + 5
            while not lines and time.time() < deadline:
                time.sleep(0.02)
        self.assertIn('v1.3.0', lines[0])
        self.assertIn('Edita a fila', lines[0])


if __name__ == '__main__':
    unittest.main()
