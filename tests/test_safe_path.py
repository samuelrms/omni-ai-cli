import isolation  # noqa: F401
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omni_ai import osutils
from omni_ai.ui import inputbox

ROOT = Path(__file__).resolve().parent.parent
MARK = 'CODE FROM THE WORKING DIRECTORY RAN'


def old_launcher(root, python='/usr/bin/python3'):
    return f'#!/usr/bin/env sh\nPYTHONPATH="{root}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{python}" -m omni_ai "$@"\n'


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.folder = self.tmp / 'untrusted'
        (self.folder / 'omni_ai').mkdir(parents=True)
        (self.folder / 'json.py').write_text(f'print("{MARK}")\n')
        (self.folder / 'omni_ai' / '__init__.py').write_text('')
        (self.folder / 'omni_ai' / '__main__.py').write_text(f'print("{MARK}")\n')
        self.env = dict(os.environ, HOME=str(self.tmp / 'home'))
        self.env.pop('PYTHONPATH', None)

    def run_here(self, argv, env=None):
        return subprocess.run(argv, cwd=self.folder, env=env or self.env, capture_output=True, text=True, timeout=30)


class WorkingDirectory(Base):
    def test_the_risk_is_real_without_the_flag(self):
        env = dict(self.env, PYTHONPATH=str(ROOT))
        self.assertIn(MARK, self.run_here([sys.executable, '-m', 'omni_ai', '--version'], env).stdout)

    def test_module_cmd_ignores_the_folder(self):
        env = osutils.module_env(self.env)
        out = self.run_here(osutils.module_cmd('cli') + ['--version'], env)
        self.assertNotIn(MARK, out.stdout)
        self.assertEqual(out.returncode, 0, out.stderr)

    @unittest.skipUnless(shutil.which('node'), 'needs node')
    def test_npm_launcher_ignores_the_folder(self):
        for pythonpath in (None, '', '/somewhere:', ':/somewhere'):
            env = dict(self.env)
            if pythonpath is not None:
                env['PYTHONPATH'] = pythonpath
            with self.subTest(pythonpath=pythonpath):
                out = self.run_here(['node', str(ROOT / 'bin' / 'ai.js'), '--version'], env)
                self.assertNotIn(MARK, out.stdout)
                self.assertEqual(out.returncode, 0, out.stderr)

    def test_module_env_drops_empty_entries(self):
        for inherited in ('', ':', '/a:', ':/a', '/a::/b'):
            with self.subTest(inherited=inherited):
                parts = osutils.module_env({'PYTHONPATH': inherited})['PYTHONPATH'].split(os.pathsep)
                self.assertNotIn('', parts)
                self.assertEqual(parts[0], str(osutils.REPO))

    def test_every_launcher_uses_the_flag(self):
        for name in ('scripts/install.sh', 'scripts/install.ps1', 'scripts/ai.cmd', 'Dockerfile', 'bin/ai.js'):
            text = (ROOT / name).read_text(encoding='utf-8')
            with self.subTest(name=name):
                self.assertTrue('-P -m omni_ai' in text or "'-P', '-m', 'omni_ai'" in text)
                self.assertNotRegex(text.replace('-P -m omni_ai', ''), r'(python3?|\$PYTHON"?|\$Python"?|py -3) -m omni_ai')


class InstalledLauncher(Base):
    def launcher(self, root, python):
        """Runs the part of install.sh that writes the launcher."""
        source = (ROOT / 'scripts' / 'install.sh').read_text()
        start = source.index("# -P keeps the working directory")
        end = source.index('chmod +x "$LINK_TARGET"')
        target = self.tmp / 'ai'
        env = dict(self.env, REPO=root, PYTHON=python, LINK_TARGET=str(target))
        subprocess.run(['bash', '-c', source[start:end]], env=env, check=True)
        target.chmod(0o755)
        return target

    def test_ignores_the_folder_and_keeps_working(self):
        target = self.launcher(str(ROOT), sys.executable)
        out = self.run_here([str(target), '--version'])
        self.assertNotIn(MARK, out.stdout)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_install_path_is_not_read_as_a_command(self):
        proof = self.folder / 'ran'  # relative in the path, and the launcher runs in this folder
        root = self.tmp / 'a$(touch ran)b c'
        root.mkdir()
        shutil.copytree(ROOT / 'omni_ai', root / 'omni_ai', ignore=shutil.ignore_patterns('__pycache__'))
        target = self.launcher(str(root), sys.executable)
        out = self.run_here([str(target), '--version'])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(proof.exists())


class Repair(Base):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / 'home'
        self.path = self.home / '.local' / 'bin' / 'ai'
        self.path.parent.mkdir(parents=True)
        patcher = patch.object(osutils.Path, 'home', return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_old_launcher_gets_the_flag_and_keeps_its_mode(self):
        self.path.write_text(old_launcher(osutils.REPO, sys.executable))
        self.path.chmod(0o755)
        osutils.repair_launcher()
        self.assertIn(' -P -m omni_ai "$@"', self.path.read_text())
        self.assertTrue(self.path.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        out = self.run_here([str(self.path), '--version'])
        self.assertNotIn(MARK, out.stdout)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_leaves_other_files_alone(self):
        for text in ('#!/bin/sh\necho mine\n', old_launcher('/another/install'), old_launcher(osutils.REPO) + 'echo extra\n'):
            with self.subTest(text=text):
                self.path.write_text(text)
                osutils.repair_launcher()
                self.assertEqual(self.path.read_text(), text)

    def test_leaves_symlinks_and_missing_files_alone(self):
        osutils.repair_launcher()
        self.assertFalse(self.path.exists())
        real = self.tmp / 'real'
        real.write_text(old_launcher(osutils.REPO))
        self.path.symlink_to(real)
        osutils.repair_launcher()
        self.assertEqual(real.read_text(), old_launcher(osutils.REPO))

    def test_windows_launcher(self):
        path = self.home / '.local' / 'bin' / 'ai.cmd'
        path.write_text(f'@echo off\nset "PYTHONPATH={osutils.REPO};%PYTHONPATH%"\n"C:\\py.exe" -m omni_ai %*\n')
        with patch.object(osutils, 'WINDOWS', True):
            osutils.repair_launcher()
        text = path.read_text()
        self.assertIn('"C:\\py.exe" -P -m omni_ai %*', text)
        self.assertIn('if defined PYTHONPATH', text)
        self.assertIn('NoDefaultCurrentDirectoryInExePath', text)


class GitConfig(Base):
    @unittest.skipUnless(shutil.which('git'), 'needs git')
    def test_mentions_do_not_run_a_command_from_the_folder(self):
        proof = self.tmp / 'ran'
        hook = self.tmp / 'hook.sh'
        hook.write_text(f'#!/bin/sh\ntouch "{proof}"\n')
        hook.chmod(0o755)
        subprocess.run(['git', 'init', '-q', str(self.folder)], check=True)
        subprocess.run(['git', '-C', str(self.folder), 'config', 'core.fsmonitor', str(hook)], check=True)
        subprocess.run(['git', '-C', str(self.folder), 'status'], capture_output=True)  # the risk is real
        self.assertTrue(proof.exists())
        proof.unlink()
        self.assertIn('json.py', inputbox.project_files(self.folder))
        self.assertFalse(proof.exists())


if __name__ == '__main__':
    unittest.main()
