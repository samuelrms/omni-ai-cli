import isolation  # noqa: F401  (first: keeps the tests away from real data)
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which('node') or next((p for p in ('/opt/homebrew/bin/node', '/usr/local/bin/node') if os.path.exists(p)), None)
BASH = shutil.which('bash') or '/bin/bash'


class Temp(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(os.path.realpath(tmp.name))
        (self.folder / 'home').mkdir()
        (self.folder / 'empty-path').mkdir()

    def launcher_env(self, **extra):
        # No Python on PATH, and nothing that could download one
        env = {'HOME': str(self.folder / 'home'), 'USERPROFILE': str(self.folder / 'home'),
               'PATH': str(self.folder / 'empty-path'), **extra}
        return env

    def downloaded(self):
        return list((self.folder / 'home').rglob('uv*'))


@unittest.skipUnless(NODE and os.name != 'nt', 'needs Node on macOS or Linux')
class PythonDownload(Temp):
    def test_without_a_terminal_nothing_is_downloaded(self):
        done = subprocess.run([NODE, str(ROOT / 'bin' / 'ai.js'), '--version'], env=self.launcher_env(),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertIn('SMART_AI_AUTO_PYTHON=1', done.stderr)
        self.assertNotIn('Instalando', done.stderr)
        self.assertEqual(self.downloaded(), [])

    def test_in_a_terminal_it_asks_and_no_means_no(self):
        import pty
        master, slave = pty.openpty()
        process = subprocess.Popen([NODE, str(ROOT / 'bin' / 'ai.js'), '--version'], env=self.launcher_env(),
                                   stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        self.addCleanup(process.kill)
        seen, answered, deadline = b'', False, time.time() + 30
        while time.time() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break  # the other side closed
                if not chunk:
                    break
                seen += chunk
            if not answered and b'[s/N]' in seen:
                os.write(master, b'n\n')
                answered = True
            if process.poll() is not None and not ready:
                break
        os.close(master)
        text = seen.decode('utf-8', 'replace')
        self.assertTrue(answered, text)
        self.assertEqual(process.wait(10), 1)
        self.assertNotIn('Instalando', text)
        self.assertEqual(self.downloaded(), [])

    def test_a_yes_given_beforehand_goes_ahead(self):
        # No sh or curl on PATH: it tries and fails here, without reaching the internet
        done = subprocess.run([NODE, str(ROOT / 'bin' / 'ai.js'), '--version'],
                              env=self.launcher_env(SMART_AI_AUTO_PYTHON='1'), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertIn('Instalando um Python 3.12', done.stderr)


@unittest.skipIf(os.name == 'nt', 'bash script')
class GetScript(Temp):
    def test_a_folder_with_something_else_is_never_removed(self):
        dest = self.folder / 'notes'
        dest.mkdir()
        (dest / 'precious.txt').write_text('keep me')
        done = subprocess.run([BASH, str(ROOT / 'get.sh')], capture_output=True, text=True, encoding='utf-8', timeout=60,
                              env={'PATH': '/usr/bin:/bin', 'HOME': str(self.folder / 'home'), 'SMART_AI_DIR': str(dest),
                                   'SMART_AI_REPO': str(self.folder / 'no-such-repo'), 'SMART_AI_NONINTERACTIVE': '1'})
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('Nada foi apagado', done.stderr)
        self.assertEqual((dest / 'precious.txt').read_text(), 'keep me')


class Downloads(unittest.TestCase):
    def test_redirects_cannot_leave_https(self):
        loose = []
        for name in ('get.sh', 'scripts/install.sh', 'bin/ai.js'):
            for number, line in enumerate((ROOT / name).read_text(encoding='utf-8').splitlines(), 1):
                code = line.split('#')[0] if name != 'bin/ai.js' else line
                if re.search(r'\bcurl\b[^|]*\s-\w*L', code) and "--proto '=https'" not in code and 'echo' not in code:
                    loose.append(f'{name}:{number}')
        self.assertEqual(loose, [])

    def test_npm_dependencies_have_an_exact_version(self):
        manifest = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
        for group in ('dependencies', 'optionalDependencies'):
            for name, version in manifest.get(group, {}).items():
                with self.subTest(package=name):
                    self.assertRegex(version, r'^\d+\.\d+\.\d+$')


if __name__ == '__main__':
    unittest.main()
