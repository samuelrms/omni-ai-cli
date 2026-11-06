import isolation  # noqa: F401
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from omni_ai.cli import commands, whats_new

spec = importlib.util.spec_from_file_location(
    'release_notes', Path(__file__).resolve().parent.parent / 'scripts' / 'release_notes.py')
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)

ENRICHED = '## [1.2.0](x)\n\n### What changes for you\n\n- Nice thing\n\n### Features\n\n* raw ([#1](u)) ([abc](u))\n'
PLAIN = '## [1.1.0](x)\n\n### Bug Fixes\n\n* a fix ([#2](u)) ([def](u))\n'


def rel(tag, body='', **extra):
    return {'tag_name': tag, 'body': body, **extra}


class Notes(unittest.TestCase):
    def test_prefers_the_user_section(self):
        self.assertEqual(whats_new.summary(ENRICHED), ['Nice thing'])

    def test_falls_back_to_generated_bullets_without_links(self):
        self.assertEqual(whats_new.summary(PLAIN), ['a fix'])

    def test_strips_terminal_control_characters(self):
        self.assertEqual(whats_new.summary('- a\x1b[31mb'), ['a[31mb'])

    def test_between_is_exclusive_below_and_skips_prereleases(self):
        releases = [rel('v1.0.0'), rel('v1.1.0', PLAIN), rel('v1.2.0', ENRICHED), rel('v1.3.0'),
                    rel('v1.2.1', prerelease=True), rel('v1.2.0-rc.1')]
        self.assertEqual([v for v, _ in whats_new.between(releases, '1.0.0', '1.2.0')], ['1.2.0', '1.1.0'])

    def test_numeric_order(self):
        releases = [rel('v1.9.0'), rel('v1.10.0')]
        self.assertEqual([v for v, _ in whats_new.between(releases, '1.8.0', '1.10.0')], ['1.10.0', '1.9.0'])

    def test_no_change_or_unknown_version_does_not_fetch(self):
        with patch.object(whats_new, 'fetch') as fetch:
            for before, after in (('1.0.0', '1.0.0'), ('1.1.0', '1.0.0'), ('1.0.0', '?'), ('?', '?')):
                self.assertEqual(whats_new.render(before, after), [])
            self.assertEqual(whats_new.render('0.0.0-development', '1.0.0'), [f'Todas as mudanças: {whats_new.RELEASES_PAGE}'])
        fetch.assert_not_called()

    def test_offline_still_gives_the_link(self):
        with patch.object(whats_new, 'fetch', side_effect=OSError):
            self.assertEqual(whats_new.render('1.0.0', '1.1.0'), [f'Todas as mudanças: {whats_new.RELEASES_PAGE}'])

    def test_limits_the_versions_shown(self):
        releases = [rel(f'v1.{n}.0', '- x') for n in range(1, 9)]
        with patch.object(whats_new, 'fetch', return_value=releases):
            text = '\n'.join(whats_new.render('1.0.0', '1.8.0'))
        self.assertIn('Novidades de 1.0.0 para 1.8.0', text)
        self.assertIn('1.8.0\n', text)
        self.assertNotIn('  1.3.0', text)
        self.assertIn('e mais 3 versões', text)


class InstalledVersion(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patch = patch.object(whats_new, 'REPO', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, version):
        (self.root / 'package.json').write_text(json.dumps({'version': version}))

    def test_published_package(self):
        self.write('1.5.0')
        self.assertEqual(whats_new.installed_version(), '1.5.0')

    def test_git_install_uses_the_tag(self):
        self.write('0.0.0-development')
        (self.root / '.git').mkdir()
        with patch.object(whats_new, 'resolve_bin', return_value='git'), \
                patch.object(whats_new.subprocess, 'run', return_value=MagicMock(stdout='v1.5.0\n')):
            self.assertEqual(whats_new.installed_version(), '1.5.0')

    def test_git_without_tags_keeps_the_placeholder(self):
        self.write('0.0.0-development')
        (self.root / '.git').mkdir()
        with patch.object(whats_new, 'resolve_bin', return_value='git'), \
                patch.object(whats_new.subprocess, 'run', return_value=MagicMock(stdout='')):
            self.assertEqual(whats_new.installed_version(), '0.0.0-development')


class RealGitUpdate(unittest.TestCase):
    """A real origin and clone, because a pull behaves differently from what mocks assume."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.origin = self.root / 'origin.git'
        self.work = self.root / 'work'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.origin)], check=True)
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(self.work)], check=True, capture_output=True)
        self.release('1.0.0')
        self.release('1.0.1')

    def git(self, folder, *args):
        return subprocess.run(['git', '-C', str(folder), '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def release(self, version):
        (self.work / 'package.json').write_text('{"version": "0.0.0-development"}')
        self.git(self.work, 'add', '-A')
        self.git(self.work, 'commit', '-q', '--allow-empty', '-m', version)
        self.git(self.work, 'tag', 'v' + version)
        self.git(self.work, 'push', '-q', 'origin', 'HEAD:main', '--tags')

    def update(self, install):
        with patch.object(whats_new, 'REPO', install), patch.object(commands, 'REPO', install), \
                patch.object(commands, 'say'):
            before = whats_new.installed_version()
            code = commands._update()
            return code, before, whats_new.installed_version()

    def test_full_clone_sees_the_new_tag(self):
        install = self.root / 'install'
        subprocess.run(['git', 'clone', '-q', '-b', 'main', str(self.origin), str(install)], check=True, capture_output=True)
        self.release('1.1.0')
        self.assertEqual(self.update(install), (0, '1.0.1', '1.1.0'))

    def test_shallow_clone_finds_its_version(self):
        install = self.root / 'install'
        subprocess.run(['git', 'clone', '-q', '--depth', '1', '-b', 'main', 'file://' + str(self.origin), str(install)],
                       check=True, capture_output=True)
        self.release('1.1.0')
        code, _, after = self.update(install)
        self.assertEqual((code, after), (0, '1.1.0'))


class ReleaseNotesScript(unittest.TestCase):
    BODY = '## O que muda\n\ntexto\n\n## Release notes\n\n<!-- help -->\n\n- New thing\n\n## Checklist\n\n- [x] ok\n'

    def test_section(self):
        self.assertEqual(release_notes.section(self.BODY), '- New thing')

    def test_missing_or_empty_section(self):
        self.assertEqual(release_notes.section('## O que muda\n'), '')
        self.assertEqual(release_notes.section('## Release notes\n\n<!-- help -->\n\n## Checklist\n'), '')
        self.assertEqual(release_notes.section(None), '')

    def test_merged_goes_after_the_title_and_only_once(self):
        once = release_notes.merged('## [1.0.0](x)\n\n### Features\n\n* raw\n', '- New')
        self.assertEqual(once, '## [1.0.0](x)\n\n### What changes for you\n\n- New\n\n### Features\n\n* raw\n')
        self.assertEqual(release_notes.merged(once, '- Other'), once)
        self.assertEqual(whats_new.summary(once), ['New'])

    def test_no_notes_keeps_the_body(self):
        self.assertEqual(release_notes.merged('body', ''), 'body')

    def test_pull_number(self):
        self.assertEqual(release_notes.pull_number('feat: thing (#80)\n'), '80')
        self.assertIsNone(release_notes.pull_number('feat: thing'))


if __name__ == '__main__':
    unittest.main()
