import isolation  # noqa: F401  (first: keeps the tests away from real data)
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
os.environ['PYTHONPATH'] = str(ROOT)
sys.path.insert(0, str(ROOT))
from omni_ai import osutils


class ModuleHelpersTests(unittest.TestCase):
    def test_module_cmd(self):
        self.assertEqual(osutils.module_cmd('cli'), [sys.executable, '-m', 'omni_ai.cli'])

    def test_module_env_from_none_adds_repo_to_pythonpath(self):
        env = osutils.module_env()
        self.assertIn(str(osutils.REPO), env['PYTHONPATH'].split(os.pathsep))

    def test_module_env_keeps_existing_pythonpath(self):
        env = osutils.module_env({'PYTHONPATH': '/already/here'})
        parts = env['PYTHONPATH'].split(os.pathsep)
        self.assertIn(str(osutils.REPO), parts)
        self.assertIn('/already/here', parts)

    def test_module_env_empty_pythonpath_not_duplicated_as_blank(self):
        env = osutils.module_env({})
        self.assertEqual(env['PYTHONPATH'], str(osutils.REPO))


class ResolveBinTests(unittest.TestCase):
    def test_absolute_path_that_exists_is_returned(self):
        with tempfile.NamedTemporaryFile() as f:
            self.assertEqual(osutils.resolve_bin(f.name), f.name)

    def test_absolute_path_that_is_missing_returns_none(self):
        self.assertIsNone(osutils.resolve_bin('/nope/does/not/exist/tool'))

    def test_bare_name_found_on_path(self):
        with patch('omni_ai.osutils.shutil.which', return_value='/usr/bin/git') as which:
            self.assertEqual(osutils.resolve_bin('git'), '/usr/bin/git')
            which.assert_called()

    def test_bare_name_found_in_extra_install_dirs(self):
        def which(name, path=None):
            return '/home/u/.local/bin/claude' if path else None
        with patch('omni_ai.osutils.shutil.which', side_effect=which):
            self.assertEqual(osutils.resolve_bin('claude'), '/home/u/.local/bin/claude')

    def test_bare_name_not_found_anywhere(self):
        with patch('omni_ai.osutils.shutil.which', return_value=None):
            self.assertIsNone(osutils.resolve_bin('ghost-binary'))


class ProcessGroupTests(unittest.TestCase):
    def test_group_kwargs_posix(self):
        with patch('omni_ai.osutils.WINDOWS', False):
            self.assertEqual(osutils.group_kwargs(), {'start_new_session': True})

    def test_group_kwargs_windows(self):
        with patch('omni_ai.osutils.WINDOWS', True), \
             patch.object(osutils.subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, create=True):
            self.assertEqual(osutils.group_kwargs(), {'creationflags': 0x200})

    def test_kill_tree_posix_term_and_force(self):
        proc = MagicMock(pid=4321)
        with patch('omni_ai.osutils.WINDOWS', False), \
             patch('omni_ai.osutils.os.killpg') as killpg:
            osutils.kill_tree(proc)
            osutils.kill_tree(proc, force=True)
        signals = [call.args[1] for call in killpg.call_args_list]
        import signal
        self.assertIn(signal.SIGTERM, signals)
        self.assertIn(signal.SIGKILL, signals)

    def test_kill_tree_posix_swallows_missing_process(self):
        proc = MagicMock(pid=1)
        with patch('omni_ai.osutils.WINDOWS', False), \
             patch('omni_ai.osutils.os.killpg', side_effect=ProcessLookupError):
            osutils.kill_tree(proc)  # must not raise

    def test_kill_tree_windows_uses_taskkill(self):
        proc = MagicMock(pid=999)
        with patch('omni_ai.osutils.WINDOWS', True), \
             patch('omni_ai.osutils.subprocess.run') as run:
            osutils.kill_tree(proc, force=True)
        argv = run.call_args.args[0]
        self.assertIn('taskkill', argv)
        self.assertIn('/F', argv)
        self.assertIn('999', argv)

    def test_kill_tree_windows_without_force_has_no_f_flag(self):
        proc = MagicMock(pid=7)
        with patch('omni_ai.osutils.WINDOWS', True), \
             patch('omni_ai.osutils.subprocess.run') as run:
            osutils.kill_tree(proc)
        self.assertNotIn('/F', run.call_args.args[0])


class LockTests(unittest.TestCase):
    def test_posix_lock_uses_flock(self):
        import fcntl
        with tempfile.NamedTemporaryFile() as f, \
             patch('omni_ai.osutils.WINDOWS', False), \
             patch('fcntl.flock') as flock:
            osutils.lock_exclusive(f)
            flock.assert_called_once()
            self.assertEqual(flock.call_args.args[1], fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_windows_lock_success(self):
        fake_msvcrt = MagicMock()
        fake_msvcrt.LK_NBLCK = 2
        stream = MagicMock()
        stream.fileno.return_value = 3
        with patch('omni_ai.osutils.WINDOWS', True), \
             patch.dict(sys.modules, {'msvcrt': fake_msvcrt}):
            osutils.lock_exclusive(stream)
        stream.seek.assert_called_once_with(0)
        fake_msvcrt.locking.assert_called_once()

    def test_windows_lock_busy_raises_blocking(self):
        fake_msvcrt = MagicMock()
        fake_msvcrt.LK_NBLCK = 2
        fake_msvcrt.locking.side_effect = OSError()
        stream = MagicMock()
        with patch('omni_ai.osutils.WINDOWS', True), \
             patch.dict(sys.modules, {'msvcrt': fake_msvcrt}):
            with self.assertRaises(BlockingIOError):
                osutils.lock_exclusive(stream)


class LinkTests(unittest.TestCase):
    def test_symlink_file(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'src.txt'
            src.write_text('data')
            dst = Path(d) / 'link.txt'
            osutils.link(dst, src)
            self.assertTrue(dst.is_symlink())
            self.assertEqual(dst.read_text(), 'data')

    def test_symlink_directory(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'src'
            src.mkdir()
            dst = Path(d) / 'link'
            osutils.link(dst, src)
            self.assertTrue(dst.is_symlink())

    def test_posix_symlink_failure_reraises(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'src.txt'
            src.write_text('data')
            dst = Path(d) / 'exists.txt'
            dst.write_text('already')  # symlink_to will raise FileExistsError
            with patch('omni_ai.osutils.WINDOWS', False):
                with self.assertRaises(OSError):
                    osutils.link(dst, src)

    def test_windows_fallback_copies_file(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'src.txt'
            src.write_text('payload')
            dst = Path(d) / 'copy.txt'
            fake_path = MagicMock()
            fake_path.symlink_to.side_effect = OSError()
            fake_path.is_dir.return_value = False
            fake_path.exists.return_value = False
            fake_path.is_symlink.return_value = False
            with patch('omni_ai.osutils.WINDOWS', True), \
                 patch('omni_ai.osutils.Path', side_effect=lambda p: fake_path), \
                 patch('omni_ai.osutils.shutil.copy2') as copy2:
                osutils.link(dst, src)
            copy2.assert_called_once()

    def test_windows_fallback_junction_for_dir(self):
        src_path = MagicMock()
        src_path.symlink_to.side_effect = OSError()
        src_path.is_dir.return_value = True
        src_path.exists.return_value = False
        src_path.is_symlink.return_value = False
        fake_winapi = MagicMock()
        with patch('omni_ai.osutils.WINDOWS', True), \
             patch('omni_ai.osutils.Path', side_effect=lambda p: src_path), \
             patch.dict(sys.modules, {'_winapi': fake_winapi}):
            osutils.link('t', 's')
        fake_winapi.CreateJunction.assert_called_once()

    def test_windows_fallback_never_clobbers_existing_target(self):
        fake_path = MagicMock()
        fake_path.symlink_to.side_effect = OSError()
        fake_path.is_dir.return_value = False
        fake_path.exists.return_value = True  # a real user file already sits at the target
        with patch('omni_ai.osutils.WINDOWS', True), \
             patch('omni_ai.osutils.Path', side_effect=lambda p: fake_path), \
             patch('omni_ai.osutils.shutil.copy2') as copy2:
            with self.assertRaises(OSError):
                osutils.link('t', 's')
        copy2.assert_not_called()


class EnableAnsiTests(unittest.TestCase):
    def test_posix_noop(self):
        with patch('omni_ai.osutils.WINDOWS', False), \
             patch('omni_ai.osutils.os.system') as system:
            osutils.enable_ansi()
            system.assert_not_called()

    def test_windows_calls_system(self):
        with patch('omni_ai.osutils.WINDOWS', True), \
             patch('omni_ai.osutils.os.system') as system:
            osutils.enable_ansi()
            system.assert_called_once_with('')


if __name__ == '__main__':
    unittest.main()
