"""Smaller guards around the administration API, the service and account removal."""
import isolation  # noqa: F401  (first: keeps the tests away from real data)
import logging
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from omni_ai.config import accounts
from omni_ai.server import admin, auth, handler, logins, service, workspaces
from omni_ai.server.protocol import RequestError


class ProtectedFields(unittest.TestCase):
    def status(self, field):
        with self.assertRaises(RequestError) as caught:
            admin._forbid_or_reject(field)
        return caught.exception.status

    def test_protected_names_get_403_in_any_case(self):
        for field in ('mode', 'MODE', 'Claude_Bin', 'config_dir', 'CONFIG_DIR', 'credentials_file', 'permission', 'permission_mode',
                      'PERMISSION_MODE', 'x_permissions_y', 'sandbox', 'Sandbox', 'some_path', 'a_file'):
            with self.subTest(field=field):
                self.assertEqual(self.status(field), 403)

    def test_other_names_get_400(self):
        for field in ('model', 'modes', 'binary', 'nopermission', 'profile', 'dirs', ''):
            with self.subTest(field=field):
                self.assertEqual(self.status(field), 400)

    def test_a_very_long_name_is_answered_at_once(self):
        field = 'permission' + '_permission' * 100_000 + '!'
        started = time.monotonic()
        self.status(field)
        self.assertLess(time.monotonic() - started, 0.5)


class SharedKeys(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, value in (('keys_path', self.root / 'keys.json'), ('admin_keys_path', self.root / 'admin.json')):
            patcher = patch.object(auth, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (auth.ENV_KEY, auth.ENV_KEY_FILE, auth.ENV_ADMIN_KEY, auth.ENV_ADMIN_KEY_FILE):
            os.environ.pop(name, None)

    def test_an_admin_key_from_the_environment_saved_for_v1(self):
        os.environ[auth.ENV_ADMIN_KEY] = auth.add('a')
        self.assertTrue(auth.admin_key_shared())

    def test_a_v1_key_from_the_environment_saved_for_admin(self):
        os.environ[auth.ENV_KEY] = auth.add('a', auth.admin_keys_path(), 'omni-admin-')
        self.assertTrue(auth.admin_key_shared())

    def test_the_same_value_in_both_variables(self):
        os.environ[auth.ENV_KEY] = os.environ[auth.ENV_ADMIN_KEY] = 'same'
        self.assertTrue(auth.admin_key_shared())

    def test_different_keys_are_fine(self):
        auth.add('a')
        auth.add('b', auth.admin_keys_path(), 'omni-admin-')
        os.environ[auth.ENV_KEY] = 'one'
        os.environ[auth.ENV_ADMIN_KEY] = 'two'
        self.assertFalse(auth.admin_key_shared())


class RequestLog(unittest.TestCase):
    def setUp(self):
        self.server = handler.Server(('127.0.0.1', 0))
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_terminal_commands_in_the_request_line_do_not_reach_the_log(self):
        with self.assertLogs(handler.REQUEST_LOG, logging.INFO) as logs:
            client = socket.create_connection(self.server.server_address, timeout=5)
            self.addCleanup(client.close)
            client.sendall(b'GET /\x1b]0;planted\x07a HTTP/1.1\r\nConnection: close\r\n\r\n')
            while client.recv(4096):
                pass
            time.sleep(0.1)
        self.assertFalse(any('\x1b' in line or '\x07' in line for line in logs.output), logs.output)
        self.assertTrue(any('\\x1b' in line for line in logs.output), logs.output)


class ServiceValues(unittest.TestCase):
    def test_line_breaks_are_refused(self):
        for values in (['a\nb'], ['a\rb'], ['a\x00b'], ['fine', 'bad\n[Service]']):
            with self.subTest(values=values), self.assertRaises(service.ServiceError):
                service.check_values(values)

    def test_normal_values_pass(self):
        service.check_values(['--host', '127.0.0.1', '/Users/me/my project', 'ação'])


class LoginAttempts(unittest.TestCase):
    def test_closing_an_old_attempt_keeps_the_new_one(self):
        table = logins.Logins()
        old, new = MagicMock(), MagicMock()
        for item in (old, new):
            item.process.poll.return_value = 0
            item.process.stdin.closed = True
        table.attempts['main'] = new
        table._close('main', old)
        self.assertIs(table.attempts['main'], new)
        table._close('main', new)
        self.assertNotIn('main', table.attempts)


class CopiesQuota(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.folder = self.root / 'project'
        self.folder.mkdir()
        (self.folder / 'file').write_text('x' * 100)
        for patcher in (patch.object(Path, 'home', return_value=self.root),):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_copies_stop_at_the_total_limit(self):
        spaces = workspaces.Workspaces(self.folder)
        first = spaces.acquire({})
        (first.path / 'more').write_text('kept')
        first.finish()
        with patch.object(workspaces, 'MAX_RUNS_BYTES', 150), self.assertRaises(RequestError) as caught:
            spaces.acquire({})
        self.assertEqual(caught.exception.status, 507)
        self.assertEqual(caught.exception.code, 'workspace_quota')

    def test_below_the_limit_it_works(self):
        spaces = workspaces.Workspaces(self.folder)
        work = spaces.acquire({})
        self.assertTrue((work.path / 'file').exists())
        work.finish()


class AccountRemoval(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.managed = self.root / 'managed'
        self.managed.mkdir()
        self.outside = self.root / 'outside'
        self.outside.mkdir()
        (self.outside / 'keep').write_text('precious')

    def test_a_path_that_climbs_out_is_not_inside(self):
        self.assertFalse(accounts.inside(self.managed / '..' / 'outside', self.managed))

    def test_a_link_that_leads_out_is_not_inside(self):
        link = self.managed / 'link'
        link.symlink_to(self.outside)
        self.assertFalse(accounts.inside(link, self.managed))

    def test_the_managed_folder_itself_is_not_inside(self):
        self.assertFalse(accounts.inside(self.managed, self.managed))

    def test_a_real_account_folder_is_inside(self):
        folder = self.managed / 'work'
        folder.mkdir()
        self.assertTrue(accounts.inside(folder, self.managed))

    def test_removing_an_edited_account_leaves_the_outside_alone(self):
        data = {'profiles': {'evil': {'config_dir': str(self.managed / '..' / 'outside')}}}
        with patch.object(accounts, 'ACCOUNTS_DIR', self.managed), patch.object(accounts, 'profiles', return_value=data), \
                patch.object(accounts, 'write_json'), patch.object(accounts.subprocess, 'run'), patch('sys.stdout'):
            accounts.cmd_account(['remove', 'evil'])
        self.assertEqual((self.outside / 'keep').read_text(), 'precious')


if __name__ == '__main__':
    unittest.main()
