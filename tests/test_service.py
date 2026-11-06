import isolation  # noqa: F401  (first: keeps the tests away from real data)
import argparse
import contextlib
import io
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from omni_ai import config  # noqa: E402
from omni_ai.server import auth, service  # noqa: E402


class Manager:
    """Stands in for launchctl, systemctl and loginctl, and for the port the service opens."""
    def __init__(self):
        self.calls, self.up, self.fail = [], False, ()

    def run(self, *args):
        self.calls.append(' '.join(args))
        code = 1 if any(word in ' '.join(args) for word in self.fail) else 0
        if not code and ('bootstrap' in args or 'enable' in args):
            self.up = True
        if 'bootout' in args or 'disable' in args:
            self.up = False
        if args[:2] == ('launchctl', 'print') and args[2].endswith(service.LABEL):
            code = 0 if self.up else 1
        return service.subprocess.CompletedProcess(args, code, 'active' if self.up else '', 'refused' if code else '')


class Base(unittest.TestCase):
    platform = 'linux'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='omni svc ')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.manager = Manager()
        for item in (patch.dict(os.environ, {'HOME': str(self.home)}),
                     patch.object(service, 'run', self.manager.run),
                     patch.object(service, 'platform_name', lambda: self.platform),
                     patch.object(service.shutil, 'which', lambda name: '/fake/' + name),
                     patch.object(service, 'port_open', lambda *_: self.manager.up),
                     patch.object(service, 'START_WAIT', 0.3),
                     patch.object(service, 'resolve_bin', lambda name: '/tools dir/' + name),
                     patch.object(service.os, 'getuid', lambda: 501, create=True),
                     patch.object(auth, 'keys_path', lambda: self.home / 'server_keys.json'),
                     patch.object(config, 'stored_keys', lambda provider: []),
                     patch.object(service.time, 'sleep', lambda _: None)):
            item.start()
            self.addCleanup(item.stop)

    def args(self, **values):
        base = dict(host='127.0.0.1', port=8080, max_concurrent=4, timeout=600, mode='plan', workdir=None, yes=False)
        return argparse.Namespace(**{**base, **values})

    def install(self, **values):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = service.install(self.args(**values), values.get('host', '127.0.0.1') == '127.0.0.1')
        return code, out.getvalue(), err.getvalue()


class Systemd(Base):
    def test_install_writes_and_enables(self):
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        text = service.service_file().read_text()
        self.assertIn(service.MARKER, text)
        self.assertIn('Restart=on-failure', text)
        self.assertIn('RestartPreventExitStatus=2', text)
        self.assertIn('--host" "127.0.0.1', text)
        self.assertIn('systemctl --user enable --now omni-ai.service', self.manager.calls)

    def test_paths_with_spaces_stay_one_argument(self):
        self.install(workdir=str(self.home / 'my work'))
        text = service.service_file().read_text()
        self.assertIn('"/tools dir/ai" "serve"', text)
        self.assertIn('"--workdir" "', text)
        self.assertTrue((self.home / 'my work').is_dir())

    def test_percent_is_escaped(self):
        self.assertEqual(service._quote_systemd('a%b$c'), '"a%%b$$c"')

    def test_new_key_is_shown_once_on_the_terminal_and_never_written(self):
        _, out, _ = self.install()
        key = out.strip().splitlines()[-1]
        self.assertTrue(key.startswith('omni-'))
        self.assertNotIn(key, service.service_file().read_text())

    def test_existing_key_is_not_shown(self):
        auth.add('mine')
        _, out, _ = self.install()
        self.assertNotIn('omni-', out.replace('omni-ai', ''))

    def test_secrets_in_the_environment_never_reach_the_file(self):
        with patch.dict(os.environ, {'OPENROUTER_API_KEY': 'sk-or-secret', 'SMART_AI_SERVER_KEY': 'server-secret', 'SMART_AI_HOME': '/cfg'}):
            self.install()
        text = service.service_file().read_text()
        self.assertNotIn('sk-or-secret', text)
        self.assertNotIn('server-secret', text)
        self.assertIn('SMART_AI_HOME=/cfg', text)

    def test_key_only_in_the_environment_does_not_count(self):
        with patch.dict(os.environ, {auth.ENV_KEY: 'env-only'}):
            _, out, _ = self.install()
        self.assertIn('Chave de API criada', out)

    def test_external_host_needs_a_saved_key(self):
        with self.assertRaisesRegex(service.ServiceError, 'exige chave'):
            self.install(host='0.0.0.0')
        self.assertFalse(service.service_file().exists())

    def test_yolo_needs_confirmation(self):
        with self.assertRaisesRegex(service.ServiceError, '--yes'):
            self.install(mode='yolo')
        self.assertEqual(self.install(mode='yolo', yes=True)[0], 0)

    def test_reinstall_replaces_instead_of_duplicating(self):
        self.install()
        self.install(port=9090)
        self.assertEqual(len(list(service.service_file().parent.iterdir())), 1)
        self.assertIn('"9090"', service.service_file().read_text())

    def test_file_not_ours_is_kept(self):
        path = service.service_file()
        path.parent.mkdir(parents=True)
        path.write_text('[Service]')
        with self.assertRaisesRegex(service.ServiceError, 'não foi gerado'):
            self.install()
        with self.assertRaisesRegex(service.ServiceError, 'não foi gerado'):
            service.uninstall(self.args())
        self.assertEqual(path.read_text(), '[Service]')

    def test_busy_port_refuses_before_writing(self):
        self.manager.up = True
        with self.assertRaisesRegex(service.ServiceError, 'em uso'):
            self.install()
        self.assertFalse(service.service_file().exists())

    def test_server_that_does_not_answer_is_not_a_success(self):
        with patch.object(service, 'port_open', lambda *_: False), self.assertRaisesRegex(service.ServiceError, 'não respondeu'):
            self.install()
        self.assertFalse(service.service_file().exists())
        self.assertIn('systemctl --user disable --now omni-ai.service', self.manager.calls)

    def test_no_user_systemd(self):
        self.manager.fail = ('show-environment',)
        with self.assertRaisesRegex(service.ServiceError, 'systemd de usuário indisponível'):
            self.install()

    def test_linger_warning_does_not_enable_it(self):
        _, _, err = self.install()
        self.assertIn('enable-linger', err)
        self.assertFalse([c for c in self.manager.calls if c.startswith('loginctl enable')])

    def test_key_only_in_env_and_missing_cli_are_named_without_values(self):
        with patch.object(config, 'stored_keys', lambda p: [('env', 'sk-secret', 'env')] if p == 'openrouter' else []), \
                patch.object(service, 'resolve_bin', lambda name: None if name == 'codex' else '/t/' + name):
            notes = ' '.join(service.warnings())
        self.assertIn('openrouter', notes)
        self.assertIn('Codex', notes)
        self.assertNotIn('sk-secret', notes)

    def test_uninstall_can_repeat_and_keeps_data(self):
        self.install()
        keys = auth.keys_path().read_text()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(service.uninstall(self.args()), 0)
            self.assertEqual(service.uninstall(self.args()), 0)
        self.assertFalse(service.service_file().exists())
        self.assertEqual(auth.keys_path().read_text(), keys)
        self.assertFalse([c for c in self.manager.calls if 'linger' in c and 'disable' in c])

    def test_status_finds_a_missing_program(self):
        self.install()
        with patch.object(service.os.path, 'exists', lambda p: p != '/tools dir/ai' and os.access(p, os.F_OK)):
            self.assertIn('não existe mais', service.describe()[1])

    def test_update_restarts_only_a_running_service(self):
        self.assertIsNone(service.after_update())
        self.install()
        self.assertTrue(service.after_update())
        self.assertIn('systemctl --user restart omni-ai.service', self.manager.calls)


class Launchd(Base):
    platform = 'darwin'

    def test_file_is_a_valid_plist(self):
        self.install(port=9000)
        data = service.service_file().read_bytes()
        self.assertIn(service.MARKER.encode(), data)
        plist = plistlib.loads(data)
        self.assertEqual(plist['Label'], service.LABEL)
        self.assertEqual(plist['ProgramArguments'][:2], ['/tools dir/ai', 'serve'])
        self.assertIn('9000', plist['ProgramArguments'])
        self.assertEqual(plist['KeepAlive'], {'SuccessfulExit': False})
        self.assertTrue(plist['RunAtLoad'])

    def test_uses_the_user_session(self):
        self.install()
        self.assertTrue([c for c in self.manager.calls if c.startswith('launchctl bootstrap gui/501 ')])

    def test_reinstall_waits_for_the_old_job_to_leave(self):
        self.install()
        leaving = iter([True, True, False])
        with patch.object(service, 'active', lambda: next(leaving, False)):
            self.assertEqual(self.install()[0], 0)
        self.assertEqual(len([c for c in self.manager.calls if 'bootstrap' in c]), 2)

    def test_no_session(self):
        self.manager.fail = ('print gui',)
        with self.assertRaisesRegex(service.ServiceError, 'launchd'):
            self.install()

    def test_status_reads_the_program(self):
        self.install()
        self.assertEqual(service._program(service.service_file()), '/tools dir/ai')


class Unsupported(Base):
    platform = 'win32'

    def test_clear_error(self):
        with self.assertRaisesRegex(service.ServiceError, 'manualmente'):
            self.install()

    def test_nothing_to_describe(self):
        self.assertIsNone(service.describe())


if __name__ == '__main__':
    unittest.main()
