import isolation  # noqa: F401  (first: keeps the tests away from real data)
from pathlib import Path
import re
import unittest

WORKFLOWS = Path(__file__).resolve().parent.parent / '.github' / 'workflows'
USES = re.compile(r'^\s*(?:-\s+)?uses:\s*(\S+)(.*)$', re.M)
# A tag can be moved to other code later; a commit cannot. The comment says which version it is.
PINNED = re.compile(r'[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}')


class Workflows(unittest.TestCase):
    def test_every_action_is_pinned_to_a_commit(self):
        loose = []
        for path in sorted(WORKFLOWS.glob('*.yml')):
            for action, rest in USES.findall(path.read_text(encoding='utf-8')):
                if action.startswith('./'):
                    continue  # this repository's own workflow
                if not PINNED.fullmatch(action) or not re.fullmatch(r'\s+# v\d+\.\d+\.\d+\s*', rest):
                    loose.append(f'{path.name}: {action}{rest}')
        self.assertEqual(loose, [])


if __name__ == '__main__':
    unittest.main()
