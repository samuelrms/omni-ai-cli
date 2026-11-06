import isolation  # noqa: F401
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from omni_ai import osutils
from omni_ai.cli import base

NPM_SHIM = """@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\\node.exe" (
  SET "_prog=%dp0%\\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\\node_modules\\tool\\cli.js" %*
"""
NATIVE_SHIM = """@ECHO off
SETLOCAL
endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%dp0%\\node_modules\\tool\\tool.exe"   %*
"""
PNPM_SHIM = """@SETLOCAL
@IF EXIST "%~dp0\\node.exe" (
  "%~dp0\\node.exe"  "%~dp0\\node_modules\\tool\\cli.js" %*
) ELSE (
  @SET PATHEXT=%PATHEXT:;.JS;=;%
  node  "%~dp0\\node_modules\\tool\\cli.js" %*
)
"""
ATTACK = '" & echo planted> proof.txt & "'


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(os.path.realpath(tmp.name))


class RecognizedShims(Temp):
    def shim(self, text, target='cli.js'):
        (self.folder / 'node_modules/tool').mkdir(parents=True)
        (self.folder / 'node_modules/tool' / target).write_text('')
        path = self.folder / 'tool.cmd'
        path.write_text(text)
        return str(path)

    def direct(self, shim, args):
        with patch.object(osutils, 'WINDOWS', True), patch.object(osutils, 'resolve_bin', return_value='/x/node'):
            return osutils.direct_argv(shim, args)

    def target(self, name):
        return str(self.folder / 'node_modules/tool' / name)

    def test_npm_shim_runs_node_with_the_script(self):
        self.assertEqual(self.direct(self.shim(NPM_SHIM), ['-p', ATTACK]), ['/x/node', self.target('cli.js'), '-p', ATTACK])

    def test_pnpm_shim_runs_node_with_the_script(self):
        self.assertEqual(self.direct(self.shim(PNPM_SHIM), [ATTACK]), ['/x/node', self.target('cli.js'), ATTACK])

    def test_native_shim_runs_the_executable(self):
        self.assertEqual(self.direct(self.shim(NATIVE_SHIM, 'tool.exe'), [ATTACK]), [self.target('tool.exe'), ATTACK])

    def test_node_beside_the_shim_comes_first(self):
        shim = self.shim(NPM_SHIM)
        (self.folder / 'node.exe').write_text('')
        self.assertEqual(self.direct(shim, ['x'])[0], str(self.folder / 'node.exe'))

    def test_node_that_is_itself_a_shim_is_not_used(self):
        shim = self.shim(NPM_SHIM)
        with patch.object(osutils, 'WINDOWS', True), patch.object(osutils, 'resolve_bin', return_value='/x/node.cmd'):
            self.assertIsNone(osutils.direct_argv(shim, [ATTACK]))

    def test_batch_extension_counts_as_a_shim(self):
        shim = self.shim(NPM_SHIM)
        renamed = os.path.join(self.folder, 'tool.BAT')
        os.rename(shim, renamed)
        self.assertEqual(self.direct(renamed, ['x'])[1], self.target('cli.js'))

    def test_outside_windows_nothing_changes(self):
        with patch.object(osutils, 'WINDOWS', False):
            self.assertEqual(osutils.direct_argv('/x/tool.cmd', [ATTACK]), ['/x/tool.cmd', ATTACK])

    def test_an_executable_is_run_as_it_is(self):
        self.assertEqual(self.direct('C:\\x\\tool.exe', [ATTACK]), ['C:\\x\\tool.exe', ATTACK])


class UnknownShims(Temp):
    def direct(self, text, args):
        path = self.folder / 'tool.cmd'
        path.write_text(text)
        with patch.object(osutils, 'WINDOWS', True):
            return osutils.direct_argv(str(path), args)

    def test_dangerous_text_is_refused(self):
        for text in (ATTACK, '%PATH%', 'a"b', 'a&b', 'a|b', 'a<b', 'a>b', 'a^b', '!x!', 'a\nb', 'a(b)'):
            with self.subTest(text=text):
                self.assertIsNone(self.direct('@echo off\nreal.exe %*\n', ['-p', text]))

    def test_plain_arguments_still_run(self):
        args = ['--version', '--cwd', 'C:\\Users\\me\\my project', 'gpt-5.5']
        self.assertEqual(self.direct('@echo off\nreal.exe %*\n', args), [str(self.folder / 'tool.cmd'), *args])

    def test_shim_with_extra_flags_is_not_trusted_as_recognized(self):
        (self.folder / 'cli.js').write_text('')
        text = '"%_prog%" --flag "%dp0%\\cli.js" %*\n'
        self.assertIsNone(self.direct(text, [ATTACK]))

    def test_shim_with_two_different_targets_is_not_recognized(self):
        (self.folder / 'a.js').write_text('')
        (self.folder / 'b.js').write_text('')
        self.assertIsNone(self.direct('node "%~dp0\\a.js" %*\nnode "%~dp0\\b.js" %*\n', [ATTACK]))

    def test_missing_script_is_not_recognized(self):
        self.assertIsNone(self.direct('node "%~dp0\\gone.js" %*\n', [ATTACK]))

    def test_unreadable_shim_is_not_recognized(self):
        with patch.object(osutils, 'WINDOWS', True):
            self.assertIsNone(osutils.direct_argv(str(self.folder / 'gone.cmd'), [ATTACK]))

    def test_run_refuses_without_starting_anything(self):
        path = self.folder / 'tool.cmd'
        path.write_text('@echo off\nreal.exe %*\n')
        with patch.object(osutils, 'WINDOWS', True), patch.object(subprocess, 'Popen') as popen, \
                patch('sys.stdout', io.StringIO()) as out:
            code, output = base.run([str(path), '-p', ATTACK])
        popen.assert_not_called()
        self.assertEqual((code, output), (126, ''))
        self.assertIn(str(path), out.getvalue())


class FindingTools(Temp):
    def test_working_directory_and_relative_folders_are_not_searched(self):
        (self.folder / 'tool.cmd').write_text('')
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.folder)
        with patch.object(osutils, 'WINDOWS', True), patch.dict(os.environ, {'PATH': '.;tools;', 'PATHEXT': '.CMD'}):
            self.assertIsNone(osutils._which('tool', path='.' + os.pathsep + 'tools' + os.pathsep))

    def test_first_folder_on_path_wins_and_suffix_is_added(self):
        first, second = self.folder / 'first', self.folder / 'second'
        first.mkdir()
        second.mkdir()
        (first / 'tool.cmd').write_text('')
        (second / 'tool.cmd').write_text('')
        with patch.object(osutils, 'WINDOWS', True), patch.dict(os.environ, {'PATHEXT': '.EXE;.cmd'}):
            found = osutils._which('tool', path=os.pathsep.join((str(first), str(second))))
        self.assertEqual(found, str(first / 'tool.cmd'))

    def test_a_name_with_a_suffix_is_found_as_it_is(self):
        (self.folder / 'tool.cmd').write_text('')
        with patch.object(osutils, 'WINDOWS', True), patch.dict(os.environ, {'PATHEXT': '.EXE'}):
            self.assertEqual(osutils._which('tool.cmd', path=str(self.folder)), str(self.folder / 'tool.cmd'))


@unittest.skipUnless(os.name == 'nt', 'needs cmd.exe')
class RealWindows(Temp):
    TEXTS = (ATTACK, '"&echo planted>proof.txt&"', '%PATH%', 'a"b', 'c:\\folder\\', '^&', '!x!', 'one\ntwo', 'ação 日本語', '', '-x')

    def setUp(self):
        super().setUp()
        package = self.folder / 'package'
        package.mkdir()
        (package / 'package.json').write_text(json.dumps({'name': 'omni-echo', 'version': '1.0.0', 'bin': {'omni-echo': 'cli.js'}}))
        (package / 'cli.js').write_text('#!/usr/bin/env node\nconsole.log(JSON.stringify(process.argv.slice(2)));\n')
        prefix = self.folder / 'prefix'
        subprocess.run([shutil.which('npm'), 'install', '-g', '--prefix', str(prefix), str(package)], check=True, capture_output=True)
        self.shim = str(prefix / 'omni-echo.cmd')
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.folder)
        self.proof = self.folder / 'proof.txt'

    def test_real_shim_is_recognized(self):
        self.assertEqual(os.path.basename(osutils.direct_argv(self.shim, [])[1]), 'cli.js')

    def test_the_risk_is_real_through_the_shim(self):
        subprocess.run([self.shim, '-p', ATTACK], capture_output=True)
        self.assertTrue(self.proof.exists())

    def test_text_reaches_the_tool_unchanged_and_runs_nothing(self):
        for text in self.TEXTS:
            with self.subTest(text=text), patch('sys.stdout', io.StringIO()):
                code, output = base.run([self.shim, '-p', text])
                self.assertEqual(code, 0, output)
                self.assertEqual(json.loads(output), ['-p', text])
                self.assertFalse(self.proof.exists())

    def test_planted_tool_in_the_working_directory_is_not_picked(self):
        (self.folder / 'omni-planted.cmd').write_text('@echo planted\n')
        self.assertIsNone(osutils.resolve_bin('omni-planted'))

    def without_protection(self, env=None):
        return {key: value for key, value in (env or os.environ).items() if key != 'NoDefaultCurrentDirectoryInExePath'}

    def plant(self, name):
        shutil.copy(shutil.which('node'), self.folder / name)

    def test_the_risk_is_real_for_a_bare_name(self):
        self.plant('git.exe')
        code = "import subprocess; print(subprocess.run(['git', '--version'], capture_output=True, text=True).stdout)"
        out = subprocess.run([sys.executable, '-c', code], env=self.without_protection(), capture_output=True, text=True).stdout
        self.assertTrue(out.startswith('v'), out)

    def test_a_bare_name_ignores_the_working_directory(self):
        self.plant('git.exe')
        out = subprocess.run(['git', '--version'], capture_output=True, text=True).stdout
        self.assertTrue(out.startswith('git version'), out)

    def launcher_env(self):
        marker = self.folder / 'planted-ran'
        script = self.folder / 'mark.js'
        script.write_text("const fs = require('fs'), path = require('path');\n"
                          f"if (path.dirname(process.execPath) === {json.dumps(str(self.folder))}) fs.writeFileSync({json.dumps(str(marker))}, 'x');\n")
        return dict(os.environ, NODE_OPTIONS=f'--require={script}'), marker

    def test_the_risk_is_real_for_the_node_launcher(self):
        self.plant('python.exe')
        env, marker = self.launcher_env()
        node = shutil.which('node')
        subprocess.run([node, '-e', "require('child_process').spawnSync('python', ['--version'])"], env=self.without_protection(env), capture_output=True)
        self.assertTrue(marker.exists())

    def test_the_node_launcher_ignores_the_working_directory(self):
        self.plant('python.exe')
        self.plant('py.exe')
        env, marker = self.launcher_env()
        launcher = Path(__file__).resolve().parent.parent / 'bin' / 'ai.js'
        node = shutil.which('node')
        out = subprocess.run([node, str(launcher), '--version'], env=self.without_protection(env), capture_output=True, text=True)
        self.assertFalse(marker.exists())
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_planted_node_in_the_working_directory_is_not_picked(self):
        (self.folder / 'node.cmd').write_text('@echo planted\n')
        self.assertNotEqual(Path(osutils.resolve_bin('node')).parent, self.folder)


if __name__ == '__main__':
    unittest.main()
