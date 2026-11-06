"""Subscription login driven over HTTP: one tool process per attempt, kept in memory, never echoed or logged."""
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time

from .. import config as config_tool
from ..osutils import group_kwargs, kill_tree, resolve_bin
from .auth import SECRET_VARS
from .protocol import RequestError

ATTEMPT_LIFETIME = 600
START_WAIT = 20
CODE_WAIT = 45
MAX_CODE = 2048
CODE = re.compile(r'[\x21-\x7e]+')
ANSI = re.compile(r'\x1b\]8;;|\x1b\\|\x1b\[[0-9;?]*[A-Za-z]')
URL = re.compile(r'https://[^\s\x07\x1b]+')
REJECTED = re.compile(r'invalid code', re.I)  # Claude says this and keeps asking, without exiting
USER_CODE = re.compile(r'\b[A-Z0-9]{4}-[A-Z0-9]{4,}\b')
PASTES_CODE = ('claude',)  # the others finish by themselves once the browser step is done
SHOWS_USER_CODE = ('codex', 'grok')


class Attempt:
    def __init__(self, account, tool, process, on_success):
        self.id = 'login_' + secrets.token_urlsafe(16)
        self.account, self.tool, self.process, self.on_success = account, tool, process, on_success
        self.expires = time.monotonic() + ATTEMPT_LIFETIME
        self.url = self.user_code = None
        self.submitted = False
        self.ready = threading.Event()
        self.done = threading.Event()
        self.answered = threading.Event()  # a pasted code got its verdict
        self.rejected = False

    def read(self):
        """The output has one-time codes: it only fills these two fields and is then dropped."""
        try:
            for raw in self.process.stdout:
                line = ANSI.sub('', raw)
                if self.submitted and REJECTED.search(line):
                    self.rejected = True
                    self.answered.set()
                self.url = self.url or next(iter(URL.findall(line)), None)
                self.user_code = self.user_code or next(iter(USER_CODE.findall(line)), None)
                if self.url and (self.user_code or self.tool not in SHOWS_USER_CODE):
                    self.ready.set()
        except (OSError, ValueError):
            pass
        self.process.wait()
        self.ready.set()
        self.done.set()
        self.answered.set()


def resolve(account):
    """(tool, extra environment, setup to share after success) for an account id, or a clear error."""
    tool, _, name = account.partition('.')
    if tool in config_tool.MAIN_LOGINS and tool != 'claude':
        if not name:
            return tool, {}, None
        conf = dict(config_tool.tool_accounts(tool)).get(name)
        if not conf:
            raise RequestError(404, 'Account not found.')
        folder = Path(conf['config_dir']).expanduser()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return tool, config_tool.tool_env(tool, conf), lambda: config_tool.share_tool_setup(tool, folder)
    if tool in config_tool.KEYS and tool != 'claude':
        raise RequestError(400, f'{tool} has no subscription login. Use an API key.', code='login_not_supported')
    conf = config_tool.profiles()['profiles'].get(account)
    if conf is None:
        raise RequestError(404, 'Account not found.')
    if conf.get('config_dir'):
        folder = Path(conf['config_dir']).expanduser()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return 'claude', {'CLAUDE_CONFIG_DIR': str(folder)}, lambda: config_tool.share_claude_setup(folder)
    legacy = conf.get('credentials_file')
    if legacy and Path(legacy).expanduser() != Path('~/.claude/.credentials.json').expanduser():
        raise RequestError(400, 'This account swaps a credentials file and cannot log in through the API.', code='login_not_supported')
    return 'claude', {}, None


def _command(tool):
    command = list(config_tool.MAIN_LOGINS[tool])
    if tool == 'codex':
        command.append('--device-auth')  # the default waits on a localhost callback this machine cannot receive
    binary = resolve_bin(os.environ.get(tool.upper() + '_BIN', command[0]))
    if not binary:
        raise RequestError(400, f'{tool} is not installed.', code='login_not_supported')
    return [binary] + command[1:]


class Logins:
    def __init__(self):
        self.lock = threading.Lock()
        self.attempts = {}

    def _drop_finished(self):
        now = time.monotonic()
        for account, attempt in list(self.attempts.items()):
            if attempt.done.is_set() or now >= attempt.expires:
                self._close(account, attempt)

    def _close(self, account, attempt):
        self.attempts.pop(account, None)
        if attempt.process.poll() is None:
            kill_tree(attempt.process, force=True)
        if attempt.process.stdin and not attempt.process.stdin.closed:
            try:
                attempt.process.stdin.close()
            except OSError:
                pass

    def start(self, account):
        tool, extra, on_success = resolve(account)
        command = _command(tool)
        env = {**os.environ, **extra, 'BROWSER': '/bin/false', 'NO_COLOR': '1'}
        for name in SECRET_VARS:
            env.pop(name, None)
        with self.lock:
            self._drop_finished()
            if account in self.attempts:
                raise RequestError(409, 'A login is already in progress for this account.', code='login_in_progress')
            try:
                process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, encoding='utf-8', errors='replace', **group_kwargs())
            except OSError:
                raise RequestError(502, f'Could not start the {tool} login.', 'api_error') from None
            attempt = Attempt(account, tool, process, on_success)
            self.attempts[account] = attempt
        threading.Thread(target=attempt.read, daemon=True).start()
        timer = threading.Timer(ATTEMPT_LIFETIME + 1, self._expire, (account, attempt))
        timer.daemon = True
        timer.start()
        attempt.ready.wait(START_WAIT)
        if not attempt.url:
            with self.lock:
                self._close(account, attempt)
            raise RequestError(502, f'The {tool} login did not give an authorization link.', 'api_error', code='login_failed')
        body = {'attempt_id': attempt.id, 'tool': tool, 'authorization_url': attempt.url, 'expires_in': ATTEMPT_LIFETIME,
                'needs_code': tool in PASTES_CODE}
        if attempt.user_code:
            body['user_code'] = attempt.user_code  # typed in the browser: this one is meant to be shown
        return body

    def _expire(self, account, attempt):
        with self.lock:
            if self.attempts.get(account) is attempt:
                self._close(account, attempt)

    def _find(self, account, attempt_id):
        with self.lock:
            attempt = self.attempts.get(account)
            if attempt is None or not isinstance(attempt_id, str) or not secrets.compare_digest(attempt.id, attempt_id):
                raise RequestError(404, 'Login attempt not found or expired.', code='login_not_found')
            if time.monotonic() >= attempt.expires:
                self._close(account, attempt)
                raise RequestError(410, 'The login attempt expired. Start again.', code='login_expired')
            return attempt

    def complete(self, account, body):
        """(HTTP status, body). Pasting a code waits for the result; the other tools answer 'pending' until done."""
        if not isinstance(body, dict):
            raise RequestError(400, 'The body must be an object.')
        attempt = self._find(account, body.get('attempt_id'))
        code = body.get('code')
        if attempt.tool in PASTES_CODE:
            if not isinstance(code, str) or len(code) > MAX_CODE or not CODE.fullmatch(code):
                raise RequestError(400, 'A valid code is required.', param='code')
            with self.lock:
                if attempt.submitted:
                    raise RequestError(409, 'A code was already sent for this attempt.', code='login_in_progress')
                attempt.submitted = True
                attempt.rejected = False
                attempt.answered.clear()
            try:
                attempt.process.stdin.write(code + '\n')
                attempt.process.stdin.flush()
            except (OSError, ValueError):
                pass
            attempt.answered.wait(CODE_WAIT)
            if attempt.rejected and not attempt.done.is_set():
                attempt.submitted = False  # the tool is asking again, so this attempt can take another code
                raise RequestError(400, 'The code is invalid. Check it and try again.', code='invalid_code')
        elif code is not None:
            raise RequestError(400, 'This tool does not take a code here: finish in the browser.', param='code')
        if not attempt.done.is_set():
            if attempt.tool in PASTES_CODE:
                self.cancel(account, attempt.id)
                raise RequestError(504, 'The login did not finish in time. Start again.', 'timeout_error', code='login_timeout')
            return 202, {'status': 'pending', 'expires_in': max(0, int(attempt.expires - time.monotonic()))}
        with self.lock:
            self._close(account, attempt)
        if attempt.process.returncode != 0:
            raise RequestError(400, 'The login failed: the code is invalid or the authorization was not given.', code='login_failed')
        if attempt.on_success:
            attempt.on_success()
        return 200, {'status': 'completed', 'account': account}

    def cancel(self, account, attempt_id):
        attempt = self._find(account, attempt_id)
        with self.lock:
            self._close(account, attempt)

    def stop_all(self):
        with self.lock:
            for account, attempt in list(self.attempts.items()):
                self._close(account, attempt)
