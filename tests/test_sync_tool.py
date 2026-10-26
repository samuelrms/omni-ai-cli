import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)


class SyncToolTests(unittest.TestCase):
    def test_union_dedupe_skip_app_internal_and_link_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.claude' / 'skills' / 'humanizer').mkdir(parents=True)
            (home / '.claude' / 'skills' / 'humanizer' / 'SKILL.md').write_text('x')
            (home / '.claude.json').write_text(json.dumps({'mcpServers': {
                'files': {'command': 'npx', 'args': ['-y', '@modelcontextprotocol/server-filesystem', '/a']}}}))
            (home / '.codex').mkdir()
            (home / '.codex' / 'config.toml').write_text(
                '[mcp_servers.repl]\ncommand = "/Applications/Codex.app/Contents/bin/repl"\n'
                '[mcp_servers.docs]\nurl = "https://docs.example/mcp"\n')
            (home / '.gemini' / 'config').mkdir(parents=True)
            (home / '.gemini' / 'config' / 'mcp_config.json').write_text(json.dumps({'mcpServers': {
                'fs': {'command': '/usr/local/bin/mcp-server-filesystem', 'args': ['/a']}}}))
            log = home / 'calls.log'
            bins = {}
            for tool in ('claude', 'codex', 'agy'):
                path = home / f'fake-{tool}'
                path.write_text(f'#!/bin/sh\necho "{tool} $*" >> {log}\n')
                path.chmod(0o700)
                bins[tool.upper() + '_BIN'] = str(path)
            env = dict(os.environ, SMART_AI_SYNC_HOME=str(home), SMART_AI_HOME=str(home / 'cfg'),
                       SMART_AI_CONFIG=str(home / 'profiles.json'), **bins)
            (home / 'profiles.json').write_text('{"profiles": {}}')
            result = subprocess.run([sys.executable, '-m', 'omni_ai.sync', '--yes'], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = log.read_text()
            self.assertNotIn('repl', calls)                       # app-internal server stays in Codex
            self.assertNotIn('add fs', calls)                     # same filesystem server, other name
            self.assertIn('claude mcp add-json -s user docs', calls)
            self.assertIn('agy mcp add docs https://docs.example/mcp', calls)
            self.assertIn('codex mcp add files -- npx -y @modelcontextprotocol/server-filesystem /a', calls)
            self.assertTrue((home / '.agents' / 'skills' / 'humanizer' / 'SKILL.md').exists())
            self.assertTrue((home / '.gemini' / 'antigravity' / 'skills' / 'humanizer').is_symlink())


if __name__ == '__main__':
    unittest.main()
