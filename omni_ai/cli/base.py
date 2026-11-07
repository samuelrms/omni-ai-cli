"""Shared primitives for the router: colors, config/state access, logging, cooldowns, process runner."""
import json
import os
import re
import subprocess
import threading
import sys
import time
from pathlib import Path

from .. import config as config_tool
from .. import usage
from ..osutils import (REPO, clean_terminal, direct_argv, enable_ansi, fits_command_line, module_cmd, module_env,
                       private_file, resolve_bin)
from ..tools import CLIS, KEY_NAMES, TOOLS
from .attempts import before_attempt

STATE_FILE = Path(os.environ.get('SMART_AI_STATE', '~/.cache/smart-ai/state.json')).expanduser()
LOG_FILE = Path(os.environ.get('SMART_AI_LOG', '~/.cache/smart-ai/usage.log')).expanduser()
BINS = {name: os.environ.get(name.upper() + '_BIN', TOOLS[name].bin) for name in CLIS}
# --claude or -c, --codex or -x ...
FLAGS = {**{name: name for name in TOOLS}, **{tool.flag: name for name, tool in TOOLS.items()}}
TOOL_FLAGS = '|'.join('--' + name for name in TOOLS)
KEY_LIST = '|'.join(KEY_NAMES)
MODES = ('default', 'plan', 'edits', 'auto', 'yolo')
# Outside what a process can return, so a tool's own exit 2 isn't read as a limit
RATE_LIMITED = 1000
NOT_READY = 1001  # account without login: skip it, don't stop the fallback
LIMIT_TEXT = re.compile(r'rate.?limit|too many requests|usage limit|session limit|limit reached|quota|resets? (at|in)|out of .*messages', re.I)

COLOR = sys.stdout.isatty() and not os.environ.get('NO_COLOR')
RED, GREEN, YELLOW, BLUE, CYAN, MAGENTA, BOLD, DIM, NC = (
    ('\033[0;31m', '\033[0;32m', '\033[1;33m', '\033[0;34m', '\033[0;36m', '\033[0;35m', '\033[1m', '\033[2m', '\033[0m')
    if COLOR else ('',) * 9)


def say(text=''):
    print(text, flush=True)


# ─── config and state ────────────────────────────────────────────

def config():
    return config_tool.read_json(config_tool.profiles_path(), {})


def profile_conf(name):
    return config().get('profiles', {}).get(name, {})


def state():
    return config_tool.read_json(STATE_FILE, {})


def set_state(key, value):
    config_tool.update_json(STATE_FILE, lambda data: data.update({key: value}))


def log_usage(tool, profile, status):
    # Metadata only: the prompt never goes to the log
    with private_file(LOG_FILE, 'a', tighten=True) as stream:
        stream.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] tool={tool} profile={profile} status={status}\n")


def cooldown_left(profile):
    key = f'{profile}_last_rate_limit'
    last = state().get(key)
    if not last:
        return 0
    minutes = int(profile_conf(profile).get('cooldown_minutes', 60))

    def remaining(stamp):
        return int(stamp) + minutes * 60 - int(time.time())

    left = remaining(last)
    if left > 0:
        return left

    def clear_if_unchanged(data):
        # Another process may have recorded a new rate limit since the read above: keep it.
        if data.get(key) == last:
            data[key] = ''

    current = config_tool.update_json(STATE_FILE, clear_if_unchanged).get(key)
    return max(0, remaining(current)) if current else 0


def mark_rate_limited(profile):
    set_state(f'{profile}_last_rate_limit', str(int(time.time())))
    say(f"{YELLOW}⚠  Conta '{profile}' atingiu o rate limit. Cooldown ativado.{NC}")


def mode():
    return 'plan' if os.environ.get('SMART_AI_READ_ONLY') == '1' else os.environ.get('SMART_AI_MODE', 'default')


def models():
    return config_tool.model_values()


def emit(event):
    if os.environ.get('SMART_AI_EVENTS') == '1':
        say(json.dumps(event, ensure_ascii=False))


def emit_provider(provider, profile=''):
    emit({'type': 'smart.provider', 'provider': provider, 'profile': profile})


def run(argv, env=None, stdin=None, prompt_at=None, long_prompt=None):
    """Run a CLI, stream its output live, and return (exit code, captured text).
    `argv[prompt_at]` is the prompt. When the command line would be too long for this system,
    `long_prompt(argv, prompt_at)` gives the same call with the prompt sent another way: (argv, stdin)."""
    executable = resolve_bin(argv[0])
    if not executable:
        say(f'{RED}✗ {argv[0]} não encontrado.{NC}')
        return 127, ''
    argv = before_attempt(argv, stdin, say)
    command = direct_argv(executable, argv[1:])
    if command and prompt_at is not None and not fits_command_line(command, env):
        if not long_prompt:
            say(f'{RED}✗ {argv[0]}: o pedido é longo demais para a linha de comando deste sistema.{NC}')
            return 126, ''
        argv, stdin = long_prompt(argv, prompt_at)
        command = direct_argv(executable, argv[1:])
    if not command:
        say(f'{RED}✗ {argv[0]}: este pedido não pode ser passado com segurança pelo atalho {executable}.{NC}')
        say(f'{DIM}  Instale a ferramenta com o npm ou aponte para o executável dela.{NC}')
        return 126, ''
    try:
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                   text=True, encoding='utf-8', errors='replace')
    except OSError as error:
        say(f'{RED}✗ {argv[0]} não pôde ser iniciado: {clean_terminal(error.strerror or error)}{NC}')
        return 126, ''
    writer = None
    if stdin is not None:
        # Feed stdin from a thread: writing inline can deadlock when the child fills its
        # stdout pipe before draining stdin, and it crashes with BrokenPipeError if the
        # child exits early. The reader loop below keeps stdout drained meanwhile.
        def _feed():
            try:
                process.stdin.write(stdin)
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        writer = threading.Thread(target=_feed, daemon=True)
        writer.start()
    captured = []
    try:
        for line in process.stdout:
            sys.stdout.write(clean_terminal(line, colors=True))
            sys.stdout.flush()
            captured.append(line)
    except BrokenPipeError:  # `ai ... | head`: nobody is reading, so the tool must not go on using quota
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    code = process.wait()
    if writer is not None:
        writer.join(timeout=1)
    return code, ''.join(captured)


def installed(tool):
    return resolve_bin(BINS[tool]) is not None


def header(label, note):
    say(f'{label} {DIM}({note}){NC}')
    say(f'{DIM}─────────────────────────────────────{NC}')
