import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import skills  # noqa: E402


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.project = root / 'home', root / 'project'
        (self.project / '.claude' / 'commands').mkdir(parents=True)
        (self.project / '.claude' / 'commands' / 'pr-review.md').write_text(
            '---\ndescription: >-\n  Review a PR\n  and publish\n---\nReview PR $ARGUMENTS carefully.')
        (self.home / '.agents' / 'skills' / 'humanize').mkdir(parents=True)
        (self.home / '.agents' / 'skills' / 'humanize' / 'SKILL.md').write_text('---\ndescription: Rewrite text\n---\nx')
        self.old = skills.HOME
        skills.HOME = self.home

    def tearDown(self):
        skills.HOME = self.old
        self.tmp.cleanup()

    def test_discovery_scope_and_block_description(self):
        catalog = skills.discover(self.project)
        self.assertEqual(set(catalog['pr-review']), {'claude'})
        self.assertEqual(catalog['pr-review']['claude'][2], 'projeto')
        self.assertEqual(skills.description(catalog['pr-review']['claude'][1]), 'Review a PR and publish')
        self.assertEqual(set(catalog['humanize']), {'codex'})

    def test_claude_gets_native_slash_command_others_get_inlined_text(self):
        entry = skills.discover(self.project)['pr-review']
        self.assertEqual(skills.build_prompt('pr-review', '237 240', 'claude', entry), ('/pr-review 237 240', True))
        entry = {'codex': ('command', self.project / '.claude' / 'commands' / 'pr-review.md', 'projeto')}
        prompt, raw = skills.build_prompt('pr-review', '237', 'codex', entry)
        self.assertFalse(raw)
        self.assertIn('Review PR 237 carefully.', prompt)
        prompt, _ = skills.build_prompt('humanize', 'oi', 'codex', skills.discover(self.project)['humanize'])
        self.assertIn('SKILL.md', prompt)

    def test_chat_sends_slash_command_verbatim_to_claude(self):
        fake = Path(self.tmp.name) / 'claude'
        fake.write_text('#!/bin/sh\nfor a in "$@"; do echo "ARG:$a"; done\n'
                        'echo \'{"type":"result","result":"ok"}\'\n')
        fake.chmod(0o700)
        env = dict(os.environ, CLAUDE_BIN=str(fake), SMART_AI_SYNC_HOME=str(self.home),
                   SMART_AI_SESSIONS=str(Path(self.tmp.name) / 's'), SMART_AI_HOME=str(Path(self.tmp.name) / 'cfg'),
                   SMART_AI_STATE=str(Path(self.tmp.name) / 'state'), SMART_AI_LOG=str(Path(self.tmp.name) / 'log'),
                   SMART_AI_CONFIG=str(Path(self.tmp.name) / 'profiles.json'))
        (Path(self.tmp.name) / 'profiles.json').write_text(json.dumps({'profiles': {'primary': {'priority': 1}}}))
        out = subprocess.run([sys.executable, str(ROOT / 'smart_ai.py'), '--chat'], input='/pr-review 237 240\n/exit\n',
                             cwd=self.project, env=env, capture_output=True, text=True, timeout=60).stdout
        self.assertIn('→ /pr-review via claude', out)
        self.assertIn('ARG:/pr-review 237 240', out)


if __name__ == '__main__':
    unittest.main()
