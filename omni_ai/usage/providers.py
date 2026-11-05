"""Per-provider usage readings (Claude, Codex, OpenRouter) read locally and over each API."""
import hashlib
import http.client
import json
import os
import select
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .. import config as config_tool
from ..osutils import resolve_bin
from .cache import _f, read, store, FRESH

PLANS = {'max': 'Max', 'pro': 'Pro', 'plus': 'Plus', 'team': 'Team', 'enterprise': 'Enterprise', 'free': 'Free'}

def plan_name(value):
    return PLANS.get(str(value or '').lower(), str(value or '').capitalize())


def claude_account(conf):
    """E-mail and plan of a Claude login; None when not logged in or the CLI does not answer."""
    env = dict(os.environ)
    if conf.get('config_dir'):
        env['CLAUDE_CONFIG_DIR'] = str(Path(conf['config_dir']).expanduser())
    try:
        result = subprocess.run([resolve_bin(os.environ.get('CLAUDE_BIN', 'claude')) or 'claude', 'auth', 'status', '--json'],
                                env=env, capture_output=True, text=True, timeout=20)
        status = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not status.get('loggedIn'):
        return None
    return {'email': status.get('email') or '', 'plan': plan_name(status.get('subscriptionType'))}


def codex_status(env=None, timeout=15):
    """Ask `codex app-server` for the account and its rate-limit windows."""
    try:
        proc = subprocess.Popen([resolve_bin('codex') or 'codex', 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, env=dict(os.environ, **(env or {})))
    except OSError:
        return None
    answers = {}
    try:
        for message in ({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'omni', 'version': '1'}}},
                        {'method': 'initialized', 'params': {}},
                        {'id': 2, 'method': 'account/read', 'params': {}},
                        {'id': 3, 'method': 'account/rateLimits/read', 'params': None}):
            proc.stdin.write(json.dumps(message) + '\n')
        proc.stdin.flush()
        end = time.time() + timeout
        while len(answers) < 2 and time.time() < end:
            ready, _, _ = select.select([proc.stdout], [], [], 0.5)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if not isinstance(reply, dict):
                continue
            if reply.get('id') in (2, 3):
                result = reply.get('result')
                answers[reply['id']] = result if isinstance(result, dict) else {}
    except OSError:
        pass
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)  # reap the child and let the pipes close instead of leaking fds
        except (OSError, subprocess.TimeoutExpired, AttributeError):
            pass
        for pipe in (getattr(proc, 'stdin', None), getattr(proc, 'stdout', None)):
            try:
                if pipe:
                    pipe.close()
            except (OSError, AttributeError):
                pass
    if not answers:
        return None
    account = (answers.get(2) or {}).get('account') or {}
    limits = (answers.get(3) or {}).get('rateLimits') or {}
    windows = []
    for key in ('primary', 'secondary'):
        window = limits.get(key)
        if isinstance(window, dict) and window.get('usedPercent') is not None:
            windows.append({'minutes': window.get('windowDurationMins') or 0, 'used': _f(window.get('usedPercent')),
                            'resets': window.get('resetsAt')})
    return {'email': account.get('email') or '', 'plan': plan_name(account.get('planType') or limits.get('planType')),
            'windows': windows, 'blocked': bool(limits.get('rateLimitReachedType'))}


def openrouter_credits(key, timeout=10):
    """Credits used by the saved OpenRouter key and its limit (None when the key has no limit)."""
    request = urllib.request.Request('https://openrouter.ai/api/v1/key', headers={'Authorization': f'Bearer {key}'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read()).get('data') or {}
        if not isinstance(data, dict):
            return None
        return {'used': _f(data.get('usage')), 'limit': data.get('limit'), 'free': bool(data.get('is_free_tier'))}
    except (OSError, ValueError, http.client.HTTPException):
        return None


def codex_accounts():
    """Main Codex login plus every extra account, each with its own CODEX_HOME."""
    found = [('assinatura', None)]
    for name, conf in config_tool.tool_accounts('codex', True):
        found.append((name, config_tool.tool_env('codex', conf)))
    return found


CLAUDE_USAGE_URL = 'https://api.anthropic.com/api/oauth/usage'
CLAUDE_WINDOWS = (('five_hour', 300), ('seven_day', 10080), ('seven_day_opus', 10080))

def claude_token(conf):
    """OAuth access token of a Claude login: Keychain on macOS, .credentials.json elsewhere."""
    folder = Path(conf['config_dir']).expanduser() if conf.get('config_dir') else None
    raw = ''
    if sys.platform == 'darwin':
        service = 'Claude Code-credentials'
        if folder:  # extra logins get their own Keychain entry, keyed by the config folder
            service += '-' + hashlib.sha256(str(folder).encode()).hexdigest()[:8]
        try:
            raw = subprocess.run(['security', 'find-generic-password', '-s', service, '-w'],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            raw = ''
    try:
        if not raw:
            path = (folder or Path('~/.claude').expanduser()) / '.credentials.json'
            raw = path.read_text(encoding='utf-8') if path.is_file() else ''
        return (json.loads(raw).get('claudeAiOauth') or {}).get('accessToken') or None
    except (OSError, ValueError, AttributeError):
        return None


def claude_limits(conf, timeout=10):
    """% used of each Claude window; None when there is no token or the endpoint does not answer."""
    token = claude_token(conf)
    if not token:
        return None
    request = urllib.request.Request(CLAUDE_USAGE_URL, headers={'Authorization': f'Bearer {token}',
                                                                'anthropic-beta': 'oauth-2025-04-20'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 429:  # too many usage checks: Anthropic says how long to wait
            try:
                return {'error': 'rate', 'retry': int(error.headers.get('retry-after') or 120)}
            except ValueError:
                return {'error': 'rate', 'retry': 120}
        return None
    except (OSError, ValueError):
        return None
    return claude_windows(data)


def claude_windows(data):
    windows = []
    for key, minutes in CLAUDE_WINDOWS:
        window = data.get(key)
        if not isinstance(window, dict) or window.get('utilization') is None:
            continue
        resets = None
        if window.get('resets_at'):
            try:
                resets = datetime.fromisoformat(window['resets_at'].replace('Z', '+00:00')).timestamp()
            except (ValueError, TypeError, AttributeError):
                pass
        windows.append({'minutes': minutes, 'used': _f(window.get('utilization')), 'resets': resets,
                        'label': 'opus' if key == 'seven_day_opus' else None})
    return windows


# ─── checagem: cache, paralelo, teto do roteador e alerta ─────────

def claude_reading(conf):
    windows = claude_limits(conf)
    if windows is None or isinstance(windows, dict):
        return windows
    return {'windows': windows}


def codex_reading(env=None):
    status = codex_status(env)
    return None if status is None else status
