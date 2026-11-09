"""The Docker image knows its own version, and `ai --update` inside it says how to update the image."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omni_ai.chat import versions
from omni_ai.cli import commands, whats_new

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
RELEASE = (ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8')


def stamp_step():
    """The shell commands that write the version file, as the image build runs them."""
    found = re.search(r"RUN (python3 -c '[^']*/app/package\.json[^']*' \"\$\{OMNI_VERSION\}\")", DOCKERFILE)
    return found.group(1) if found else None


@unittest.skipUnless(shutil.which('sh') and os.name != 'nt', 'needs a POSIX shell')
class Stamp(unittest.TestCase):
    def build(self, version):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        script = stamp_step().replace('/app/package.json', str(folder / 'package.json'))
        done = subprocess.run(['sh', '-c', script.replace('python3 ', shlex.quote(sys.executable) + ' ', 1)],
                              env={'OMNI_VERSION': version, 'PATH': os.environ['PATH']},
                              capture_output=True, text=True, encoding='utf-8', errors='replace')
        return done.returncode, folder

    def test_the_image_has_a_version_file(self):
        self.assertIsNotNone(stamp_step())

    def test_the_release_version_is_what_ai_shows(self):
        code, folder = self.build('1.24.26')
        self.assertEqual(code, 0)
        with patch.object(whats_new, 'REPO', folder), patch.object(versions, 'REPO', folder):
            self.assertEqual(whats_new.installed_version(), '1.24.26')
            self.assertEqual(versions.installed_version(), '1.24.26')

    def test_a_prerelease_is_accepted(self):
        self.assertEqual(self.build('1.25.0-rc.1')[0], 0)

    def test_anything_else_stops_the_build(self):
        for version in ('', 'latest', '1.2', '1.2.3"; rm -rf /', '1.2.3\n'):
            with self.subTest(version=version):
                code, folder = self.build(version)
                self.assertNotEqual(code, 0)
                self.assertFalse((folder / 'package.json').exists())


class Release(unittest.TestCase):
    def test_a_local_build_has_a_default(self):
        self.assertRegex(DOCKERFILE, r'(?m)^ARG OMNI_VERSION=0\.0\.0-development$')

    def test_the_release_passes_its_version_to_the_image(self):
        self.assertRegex(RELEASE, r'build-args: \|\n\s+OMNI_VERSION=\$\{\{ needs\.release\.outputs\.version \}\}')


class UpdateInsideTheImage(unittest.TestCase):
    def test_it_explains_how_to_update_the_image(self):
        said = []
        with patch.object(commands, 'in_container', return_value=True), \
                patch.object(commands, 'say', side_effect=lambda text='': said.append(text)), \
                patch.object(commands.subprocess, 'call') as call:
            self.assertEqual(commands._update(), 1)
        call.assert_not_called()
        text = '\n'.join(said)
        self.assertIn('docker compose pull omni', text)
        self.assertNotIn('get.sh', text)


if __name__ == '__main__':
    unittest.main()
