"""What differs between macOS, Linux and Windows, in one place."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

WINDOWS = os.name == 'nt'
REPO = Path(__file__).resolve().parent.parent


def module_cmd(name):
    """Command line that runs one of this package's modules (python -m omni_ai.<name>)."""
    return [sys.executable, '-m', 'omni_ai.' + name]


def module_env(env=None):
    """Environment in which `python -m omni_ai...` finds the package, from any working directory."""
    env = dict(os.environ if env is None else env)
    env['PYTHONPATH'] = os.pathsep.join(p for p in (str(REPO), env.get('PYTHONPATH', '')) if p)
    return env


def resolve_bin(name):
    """Full path of an executable; on Windows this finds npm's claude.cmd / codex.cmd shims."""
    if os.path.sep in name or (os.altsep and os.altsep in name):
        return name if os.path.exists(name) else None
    # Installers put tools in ~/.local/bin or ~/.grok/bin, which the shell only adds to PATH after a restart
    extra = os.pathsep.join(str(Path.home() / folder) for folder in ('.local/bin', '.grok/bin'))
    return shutil.which(name) or shutil.which(name, path=extra)


def group_kwargs():
    """Popen options that put the child in its own process group, so the whole tree can be stopped."""
    if WINDOWS:
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def kill_tree(process, force=False):
    try:
        if WINDOWS:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T'] + (['/F'] if force else []),
                           capture_output=True)
        else:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


def lock_exclusive(stream):
    """Non-blocking exclusive lock; raises BlockingIOError when another process holds it."""
    if WINDOWS:
        import msvcrt
        try:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise BlockingIOError() from None
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def link(target, source):
    """Make `target` point at `source`. Windows without symlink rights: junction for folders, copy for files."""
    target, source = Path(target), Path(source)
    try:
        target.symlink_to(source, target_is_directory=source.is_dir())
        return
    except OSError:
        if not WINDOWS:
            raise
    if source.is_dir():
        import _winapi
        _winapi.CreateJunction(str(source), str(target))
    else:
        shutil.copy2(source, target)


def enable_ansi():
    """Windows 10+ consoles render ANSI colors once VT processing is switched on."""
    if WINDOWS:
        os.system('')

