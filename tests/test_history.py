import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omni_ai import history, cli  # noqa: E402


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.omni = self.root / 'omni'
        self.claude = self.root / 'claude'
        self.codex = self.root / 'codex'
        (self.omni).mkdir()
        (self.claude / 'projects' / 'proj').mkdir(parents=True)
        (self.codex / 'sessions' / '2026').mkdir(parents=True)
        env = patch.dict(os.environ, {
            'SMART_AI_SESSIONS': str(self.omni),
            'CLAUDE_CONFIG_DIR': str(self.claude),
            'CODEX_HOME': str(self.codex),
            'CURSOR_STATE_DB': str(self.root / 'no-cursor.vscdb'),  # isolate from the real machine
        })
        env.start()
        self.addCleanup(env.stop)

    def test_reads_all_three_sources(self):
        (self.omni / 'a.json').write_text(json.dumps({
            'id': 'a', 'cwd': '/p', 'updated': 100,
            'messages': [{'role': 'user', 'content': 'oi omni'}, {'role': 'assistant', 'content': 'resp'}]}))
        (self.claude / 'projects' / 'proj' / 'b.jsonl').write_text('\n'.join([
            json.dumps({'type': 'mode', 'mode': 'normal'}),
            json.dumps({'type': 'user', 'cwd': '/c', 'message': {'role': 'user', 'content': 'oi claude'}}),
            json.dumps({'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'claude resp'}]}}),
        ]))
        (self.codex / 'sessions' / '2026' / 'c.jsonl').write_text('\n'.join([
            json.dumps({'type': 'session_meta', 'payload': {'cwd': '/x'}}),
            json.dumps({'payload': {'role': 'user', 'content': [{'type': 'text', 'text': 'oi codex'}]}}),
            json.dumps({'payload': {'role': 'assistant', 'content': 'codex resp'}}),
        ]))
        by_source = {s['source']: s for s in history.sessions()}
        self.assertEqual(set(by_source), {'omni', 'claude', 'codex'})
        self.assertEqual(by_source['omni']['title'], 'oi omni')
        self.assertEqual(by_source['omni']['count'], 2)
        self.assertEqual(by_source['claude']['title'], 'oi claude')
        self.assertEqual(by_source['codex']['title'], 'oi codex')

    def test_conversation_loads_full_messages(self):
        (self.omni / 'a.json').write_text(json.dumps({
            'id': 'a', 'messages': [{'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'a'}]}))
        conv = history.conversation('omni:a')
        self.assertEqual([m['content'] for m in conv['messages']], ['q', 'a'])
        self.assertIsNone(history.conversation('omni:missing'))

    def test_malformed_files_are_skipped(self):
        (self.omni / 'bad.json').write_text('{not json')
        (self.omni / 'ok.json').write_text(json.dumps({'id': 'ok', 'messages': [{'role': 'user', 'content': 'hi'}]}))
        ids = [s['id'] for s in history.sessions(['omni'])]
        self.assertEqual(ids, ['omni:ok'])

    def test_cursor_reader(self):
        import sqlite3
        db = self.root / 'state.vscdb'
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)')
        cid = 'abc'
        con.execute('INSERT INTO cursorDiskKV VALUES (?,?)', (f'composerData:{cid}', json.dumps({
            'composerId': cid, 'name': 'Minha conversa cursor', 'lastUpdatedAt': 5000,
            'fullConversationHeadersOnly': [{'bubbleId': 'b1', 'type': 1}, {'bubbleId': 'b2', 'type': 2}]})))
        con.execute('INSERT INTO cursorDiskKV VALUES (?,?)', (f'bubbleId:{cid}:b1', json.dumps({'type': 1, 'text': 'pergunta cursor'})))
        con.execute('INSERT INTO cursorDiskKV VALUES (?,?)', (f'bubbleId:{cid}:b2', json.dumps({'type': 2, 'text': 'resposta cursor'})))
        con.commit()
        con.close()
        with patch.dict(os.environ, {'CURSOR_STATE_DB': str(db)}):
            meta = {s['source']: s for s in history.sessions(['cursor'])}
            self.assertEqual(meta['cursor']['title'], 'Minha conversa cursor')
            self.assertEqual(meta['cursor']['count'], 2)
            conv = history.conversation('cursor:abc')
            self.assertEqual([(m['role'], m['content']) for m in conv['messages']],
                             [('user', 'pergunta cursor'), ('assistant', 'resposta cursor')])

    def test_cli_history_json(self):
        (self.omni / 'a.json').write_text(json.dumps({'id': 'a', 'messages': [{'role': 'user', 'content': 'hi'}]}))
        lines = []
        with patch.object(cli, 'say', side_effect=lambda *a: lines.append(a[0] if a else '')):
            self.assertEqual(cli.history_command(['--json', '--source', 'omni']), 0)
        data = json.loads(lines[0])
        self.assertEqual(data[0]['id'], 'omni:a')


if __name__ == '__main__':
    unittest.main()
