import isolation  # noqa: F401  (first: keeps the tests away from real data)
import json
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


    def run_blocks(self):
        """Each `run:` script, with the workflow name."""
        for path in sorted(WORKFLOWS.glob('*.yml')):
            lines = path.read_text(encoding='utf-8').splitlines()
            for index, line in enumerate(lines):
                found = re.match(r'^(\s*)(?:-\s+)?run:\s*(.*)$', line)
                if not found:
                    continue
                indent, inline = len(found.group(1)), found.group(2)
                if inline not in ('|', '>', '|-', '>-'):
                    yield path.name, inline
                    continue
                body = []
                for following in lines[index + 1:]:
                    if following.strip() and len(following) - len(following.lstrip()) <= indent:
                        break
                    body.append(following)
                yield path.name, '\n'.join(body)

    def test_scripts_take_values_through_the_environment(self):
        # ${{ }} is pasted into the script before it runs, so a value with a quote becomes a command
        self.assertEqual([name for name, script in self.run_blocks() if '${{' in script], [])

    def test_tools_are_installed_at_exact_versions(self):
        loose = []
        for name, script in self.run_blocks():
            for line in script.splitlines():
                if re.search(r'@latest\b|npx\s+--yes|npx\s+-y\b', line):
                    loose.append(f'{name}: {line.strip()}')
                if re.search(r'\bnpm\s+(install|i)\s+-g\s', line) and not re.search(r'@\d+\.\d+\.\d+|\s\./', line):  # a version, or this repository's own tarball
                    loose.append(f'{name}: {line.strip()}')
                if re.search(r'\bpip3?\s+install\b', line) and '--require-hashes' not in line:
                    loose.append(f'{name}: {line.strip()}')
        self.assertEqual(loose, [])

    def test_release_tools_come_from_the_lockfile_without_scripts(self):
        tools = WORKFLOWS.parent / 'release-tools'
        manifest = json.loads((tools / 'package.json').read_text(encoding='utf-8'))
        for name, version in manifest['dependencies'].items():
            with self.subTest(package=name):
                self.assertRegex(version, r'^\d+\.\d+\.\d+$')
        self.assertTrue((tools / 'package-lock.json').exists())
        installs = [s for _, s in self.run_blocks() if 'release-tools' in s and 'npm ci' in s]
        self.assertTrue(installs)
        for script in installs:
            self.assertIn('--ignore-scripts', script)


if __name__ == '__main__':
    unittest.main()
