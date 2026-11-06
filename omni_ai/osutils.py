"""What differs between macOS, Linux and Windows, in one place."""
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys

WINDOWS = os.name == 'nt'
REPO = Path(__file__).resolve().parent.parent
# Python puts the working directory first on sys.path, which runs any json.py or omni_ai/ found there.
# Only a flag: PYTHONSAFEPATH would also reach the user's own scripts run by the tools.
SAFE_PATH = ['-P'] if sys.version_info >= (3, 11) else []
OLD_POSIX_LAUNCHER = re.compile(
    r'#!/usr/bin/env sh\nPYTHONPATH="(?P<root>[^\n]*)\$\{PYTHONPATH:\+:\$PYTHONPATH\}" exec "[^\n]*" -m omni_ai "\$@"\n')
OLD_WINDOWS_LAUNCHER = re.compile(
    r'@echo off\nset "PYTHONPATH=(?P<root>[^\n]*);%PYTHONPATH%"\n"(?P<python>[^\n]*)" -m omni_ai %\*\n')
if WINDOWS:
    os.environ['NoDefaultCurrentDirectoryInExePath'] = '1'  # keeps the working directory out of executable lookup


def module_cmd(name):
    """Command line that runs one of this package's modules (python -m omni_ai.<name>)."""
    return [sys.executable, *SAFE_PATH, '-m', 'omni_ai.' + name]


def repair_launcher():
    """`ai --update` doesn't rewrite the launcher made at install time, so fix an old one we generated."""
    if not SAFE_PATH:
        return
    path = Path.home() / '.local' / 'bin' / ('ai.cmd' if WINDOWS else 'ai')
    try:
        if path.is_symlink():
            return
        text = path.read_text(encoding='utf-8')
        found = (OLD_WINDOWS_LAUNCHER if WINDOWS else OLD_POSIX_LAUNCHER).fullmatch(text)
        if not found or found['root'] != str(REPO):
            return
        if WINDOWS:
            root, python = found['root'], found['python']
            text = ('@echo off\nset "NoDefaultCurrentDirectoryInExePath=1"\n'
                    f'if defined PYTHONPATH (set "PYTHONPATH={root};%PYTHONPATH%") else (set "PYTHONPATH={root}")\n'
                    f'"{python}" -P -m omni_ai %*\n')
        else:
            text = text.replace(' -m omni_ai ', ' -P -m omni_ai ')
        temporary = path.with_name(path.name + '.new')
        temporary.write_text(text, encoding='utf-8')
        temporary.chmod(path.stat().st_mode)
        temporary.replace(path)
    except OSError:
        pass


def module_env(env=None):
    """Environment in which `python -m omni_ai...` finds the package, from any working directory."""
    env = dict(os.environ if env is None else env)
    # An empty entry means the working directory, even with SAFE_PATH
    inherited = env.get('PYTHONPATH', '').split(os.pathsep)
    env['PYTHONPATH'] = os.pathsep.join(p for p in (str(REPO), *inherited) if p)
    return env


def resolve_bin(name):
    """Full path of an executable; on Windows this finds npm's claude.cmd / codex.cmd shims."""
    if os.path.sep in name or (os.altsep and os.altsep in name):
        return name if os.path.exists(name) else None
    # Installers put tools in ~/.local/bin or ~/.grok/bin, which the shell only adds to PATH after a restart
    extra = os.pathsep.join(str(Path.home() / folder) for folder in ('.local/bin', '.grok/bin'))
    return shutil.which(name) or shutil.which(name, path=extra)


def in_container():
    return Path('/.dockerenv').exists()


def headless():
    """No browser to reach a login's localhost callback: SSH, a container, or Linux without a display.
    SMART_AI_HEADLESS=1 or 0 overrides the detection."""
    forced = os.environ.get('SMART_AI_HEADLESS')
    if forced in ('0', '1'):
        return forced == '1'
    if os.environ.get('SSH_CONNECTION') or os.environ.get('SSH_TTY') or in_container():
        return True
    return sys.platform.startswith('linux') and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


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
        # Never let the copy/junction fallback clobber an existing file at the target.
        if target.exists() or target.is_symlink():
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

