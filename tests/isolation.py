"""Keeps the test suite away from the developer's real data. Every test module imports this first.

Paths like ~/.config/smart-ai/keys are resolved when omni_ai is imported, so the environment has to change before
that. Patching them test by test is not enough: one missed binding once overwrote real API keys.
"""
import atexit
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

REAL_HOME_VAR = 'OMNI_TEST_REAL_HOME'  # survives into child processes, whose HOME is already the fake one
REAL_HOME = Path(os.environ.get(REAL_HOME_VAR) or os.path.expanduser('~')).resolve()
REPO = Path(__file__).resolve().parents[1]
SECRET_OR_LOCATION = re.compile(r'^(SMART_AI_|XDG_(CONFIG|CACHE|DATA|STATE)_)|_API_KEY$|_BIN$|^(CLAUDE_CONFIG_DIR|CODEX_HOME|GROK_HOME)$')
violations = []


def _allowed(path):
    return any(path == root or root in path.parents for root in (
        REPO, Path(tempfile.gettempdir()).resolve(), Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()))


def _inside_real_home(raw):
    if not isinstance(raw, (str, bytes, os.PathLike)):
        return None
    path = Path(os.path.realpath(os.fsdecode(raw)))
    return path if (REAL_HOME == path or REAL_HOME in path.parents) and not _allowed(path) else None


def _guard(event, args):
    """Refuses, and fails the run for, any change to the real home folder made from this process."""
    if event == 'open':
        writes = isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        targets = args[:1] if writes else ()
    elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.link', 'os.symlink'):
        targets = args[:1]
    elif event == 'os.rename':
        targets = args[:2]
    else:
        return
    for target in targets:
        path = _inside_real_home(target)
        if path and path.suffix != '.pyc':
            violations.append(f'{event}: {path}')
            raise PermissionError(f'test tried to change the real home folder: {path}')


def _finish(fake_home):
    shutil.rmtree(fake_home, ignore_errors=True)
    if violations:
        sys.stdout.flush()
        print('\nTests touched the real home folder:\n  ' + '\n  '.join(sorted(set(violations))), file=sys.stderr, flush=True)
        os._exit(1)


if not os.environ.get('OMNI_TEST_ISOLATED_PID') == str(os.getpid()):
    os.environ['OMNI_TEST_ISOLATED_PID'] = str(os.getpid())
    os.environ[REAL_HOME_VAR] = str(REAL_HOME)
    for name in list(os.environ):
        if SECRET_OR_LOCATION.search(name) and name != 'SMART_AI_SIM_URL':  # the simulator is local and opt-in
            del os.environ[name]
    fake = tempfile.mkdtemp(prefix='omni-test-home-')
    os.environ['HOME'] = os.environ['USERPROFILE'] = fake
    if os.name != 'nt':
        # Without this, tests that forget a fake CLI run the real Claude, Codex or Cursor, with real accounts
        system = ['/usr/bin', '/bin', '/usr/sbin', '/sbin']
        os.environ['PATH'] = os.pathsep.join([os.path.dirname(sys.executable)] + system)
    sys.addaudithook(_guard)
    atexit.register(_finish, fake)
