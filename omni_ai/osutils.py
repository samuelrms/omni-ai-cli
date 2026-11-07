"""What differs between macOS, Linux and Windows, in one place."""
import codecs
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading

WINDOWS = os.name == 'nt'
REPO = Path(__file__).resolve().parent.parent
# Python puts the working directory first on sys.path, which runs any json.py or omni_ai/ found there.
# Only a flag: PYTHONSAFEPATH would also reach the user's own scripts run by the tools.
SAFE_PATH = ['-P'] if sys.version_info >= (3, 11) else []
OLD_POSIX_LAUNCHER = re.compile(
    r'#!/usr/bin/env sh\nPYTHONPATH="(?P<root>[^\n]*)\$\{PYTHONPATH:\+:\$PYTHONPATH\}" exec "[^\n]*" -m omni_ai "\$@"\n')
OLD_WINDOWS_LAUNCHER = re.compile(
    r'@echo off\nset "PYTHONPATH=(?P<root>[^\n]*);%PYTHONPATH%"\n"(?P<python>[^\n]*)" -m omni_ai %\*\n')
# The last line of a shim: optionally Node, then a path next to the shim, then every argument
_SHIM_LAUNCH = re.compile(r'''(?:("%_prog%"|"%~?dp0%?\\node\.exe"|\bnode(?:\.exe)?)\s+)?"%~?dp0%?\\([^"%\r\n]+)"\s+%\*[ \t]*\r?$''', re.M)
_CMD_SAFE = re.compile(r'[A-Za-z0-9_./:=@,+\\ -]*')
if WINDOWS:
    os.environ['NoDefaultCurrentDirectoryInExePath'] = '1'  # keeps the working directory out of executable lookup


# Terminal commands in text from models, tools or files: they can write to the clipboard, retitle the window
# or hide lines. Stateless on purpose: removing every ESC also defeats a sequence split across two pieces.
_SEQUENCE = re.compile(r'\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)?|\x1b[PX^_][^\x1b\n]*(?:\x1b\\)?|[\x9d\x90\x98\x9e\x9f][^\x07\x1b\x9c\n]*[\x07\x9c]?'
                       r'|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b[ -/]*[0-~]?')
_COLOR = re.compile(r'(?:\x1b\[|\x9b)[0-9;:]*m')
_CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')


def clean_terminal(text, colors=False):
    """Text that is safe to print. `colors` keeps color codes, for a tool's own output in direct use."""
    text = str(text)
    return _keep_colors(text) if colors else _CONTROL.sub('', _SEQUENCE.sub('', text))


def _keep_colors(text):
    pieces, position = [], 0
    for found in _SEQUENCE.finditer(text):
        pieces.append(_CONTROL.sub('', text[position:found.start()]))
        if _COLOR.fullmatch(found.group()):
            pieces.append(found.group())
        position = found.end()
    pieces.append(_CONTROL.sub('', text[position:]))
    return ''.join(pieces)


def module_cmd(name):
    """Command line that runs one of this package's modules (python -m omni_ai.<name>).
    UTF-8 mode: on Windows a pipe would otherwise use the ANSI code page, which cannot write ✓ or most emoji."""
    return [sys.executable, *SAFE_PATH, '-X', 'utf8', '-m', 'omni_ai.' + name]


def utf8_stdio():
    """Standard streams in UTF-8. Windows pipes and C locales start in a code page that fails on the first ✓,
    and the editor and the server read the output as UTF-8."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            if codecs.lookup(stream.encoding).name != 'utf-8':
                stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, LookupError, TypeError, ValueError, OSError):
            pass  # replaced by something that is not a text stream, or already closed


def send_prompt(process, text):
    """Writes the prompt to a child started with `--prompt-file -` and closes its stdin.
    The router reads all of it before printing anything, so writing first cannot fill the other pipe."""
    try:
        process.stdin.write(text)
        process.stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        pass  # it ended before reading: its exit code and output say why


# Linux refuses a single argument over 128 KiB; Windows a whole command line over 32,767 characters
_LONGEST_ARGUMENT = 131_072
_WINDOWS_COMMAND_LINE = 32_767


def fits_command_line(command, env=None):
    """Whether this system can start `command`. Too long and starting it fails, whatever the tool."""
    if WINDOWS:
        return len(subprocess.list2cmdline(command)) < _WINDOWS_COMMAND_LINE
    sizes = [len(os.fsencode(part)) + 1 for part in command]
    if sys.platform.startswith('linux') and max(sizes, default=0) > _LONGEST_ARGUMENT:
        return False
    try:
        limit = os.sysconf('SC_ARG_MAX')
    except (AttributeError, ValueError, OSError):
        limit = 262_144
    env = os.environ if env is None else env
    environment = sum(len(os.fsencode(key)) + len(os.fsencode(value)) + 2 for key, value in env.items())
    pointers = 8 * (len(command) + len(env) + 2)
    return sum(sizes) + environment + pointers + 4096 < limit


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


def _which(name, path=None):
    if not WINDOWS:
        return shutil.which(name, path=path)
    # Python 3.11 always searches the working directory on Windows, where a repository can plant a claude.cmd
    folders = (os.environ.get('PATH', '') if path is None else path).split(os.pathsep)
    suffixes = ['', *os.environ.get('PATHEXT', '.COM;.EXE;.BAT;.CMD').split(';')]
    for folder in filter(os.path.isabs, (folder.strip('"') for folder in folders)):
        for suffix in suffixes[1 if not Path(name).suffix else 0:]:
            found = os.path.join(folder, name + suffix)
            if os.path.isfile(found):
                return found
    return None


def resolve_bin(name):
    """Full path of an executable; on Windows this finds npm's claude.cmd / codex.cmd shims."""
    if os.path.sep in name or (os.altsep and os.altsep in name):
        return name if os.path.exists(name) else None
    # Installers put tools in ~/.local/bin or ~/.grok/bin, which the shell only adds to PATH after a restart
    extra = os.pathsep.join(str(Path.home() / folder) for folder in ('.local/bin', '.grok/bin'))
    return _which(name) or _which(name, path=extra)


def _is_shim(path):
    return Path(path).suffix.lower() in ('.cmd', '.bat')


def _shim_command(shim):
    """What an npm, pnpm or yarn .cmd shim really launches, or None when it is not one we recognize."""
    try:
        text = Path(shim).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    launches = {(bool(runner), target) for runner, target in _SHIM_LAUNCH.findall(text)}
    if len(launches) != 1:
        return None
    needs_node, target = launches.pop()
    folder = os.path.dirname(shim)
    target = os.path.normpath(os.path.join(folder, target.replace('\\', os.sep)))
    if not os.path.isfile(target):
        return None
    if not needs_node:
        return [target] if target.lower().endswith('.exe') else None
    beside = os.path.join(folder, 'node.exe')
    node = beside if os.path.isfile(beside) else resolve_bin('node')
    return [node, target] if node and not _is_shim(node) else None


def direct_argv(executable, args):
    """The command line for a tool, or None when it cannot be run safely with these arguments.
    Windows hands a .cmd to cmd.exe, whose rules differ from the quoting Python applies, so text in `args`
    could become a command. Running what the shim launches skips cmd.exe altogether."""
    if not WINDOWS or not _is_shim(executable):
        return [executable, *args]
    command = _shim_command(executable)
    if command:
        return [*command, *args]
    return [executable, *args] if all(_CMD_SAFE.fullmatch(arg) for arg in args) else None


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
            if not force:
                try:
                    # taskkill without /F only asks a program's windows to close; console tools have none, so it did nothing.
                    # Ctrl+Break reaches the whole group group_kwargs made, which is what Ctrl+C does in a terminal.
                    os.kill(process.pid, signal.CTRL_BREAK_EVENT)
                    return
                except OSError:
                    pass  # no console to send it through: stop it for real
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
        else:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass


def private_file(path, mode='w', encoding=None, tighten=False):
    """Opens a file only its owner can read, whatever the umask. `tighten` also fixes a file made loose earlier;
    left off, an existing file the user chose keeps the permissions they gave it."""
    path = Path(path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if 'a' in mode else os.O_TRUNC)
    descriptor = os.open(path, flags | getattr(os, 'O_BINARY', 0), 0o600)
    if tighten:
        tighten_file(path)
    return os.fdopen(descriptor, mode, encoding=None if 'b' in mode else encoding or 'utf-8')


def tighten_file(path):
    try:
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o600)
    except OSError:
        pass  # not ours to change: the file still works


def stop_tree(process, force_after):
    """Asks the whole tree to stop, then kills what is left. Always both: the parent dying says nothing about
    the tool under it, which can ignore the first signal and keep running."""
    kill_tree(process)
    timer = threading.Timer(force_after, kill_tree, args=(process,), kwargs={'force': True})
    timer.daemon = True
    timer.start()


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

