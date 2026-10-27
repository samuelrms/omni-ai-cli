"""Real usage numbers for /usage: who each account is and how much of its limit is used.

Each tool is asked through its own channel, so no credential is read or copied here:
  claude      `claude auth status --json` (e-mail and plan of each login) and the usage endpoint that
              Claude Code's own /usage calls (% of the 5-hour and weekly windows); the login token is
              read from the Keychain or .credentials.json only in memory and only sent to Anthropic
  codex       `codex app-server` (e-mail, plan, % of the 5-hour and weekly windows, reset time)
  openrouter  GET /api/v1/key with the saved key (credits used and the key's limit)
Anything that fails returns None and /usage falls back to what the tool last said.
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import urllib.request

from . import config as config_tool
from .osutils import resolve_bin

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
            if reply.get('id') in (2, 3):
                answers[reply['id']] = reply.get('result') or {}
    except OSError:
        pass
    finally:
        proc.kill()
    if not answers:
        return None
    account = (answers.get(2) or {}).get('account') or {}
    limits = (answers.get(3) or {}).get('rateLimits') or {}
    windows = []
    for key in ('primary', 'secondary'):
        window = limits.get(key)
        if window and window.get('usedPercent') is not None:
            windows.append({'minutes': window.get('windowDurationMins') or 0, 'used': float(window['usedPercent']),
                            'resets': window.get('resetsAt')})
    return {'email': account.get('email') or '', 'plan': plan_name(account.get('planType') or limits.get('planType')),
            'windows': windows, 'blocked': bool(limits.get('rateLimitReachedType'))}


def openrouter_credits(key, timeout=10):
    """Credits used by the saved OpenRouter key and its limit (None when the key has no limit)."""
    request = urllib.request.Request('https://openrouter.ai/api/v1/key', headers={'Authorization': f'Bearer {key}'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read()).get('data') or {}
    except (OSError, ValueError):
        return None
    return {'used': float(data.get('usage') or 0), 'limit': data.get('limit'), 'free': bool(data.get('is_free_tier'))}


def window_label(minutes, label=None):
    if label:
        return label
    if minutes and minutes % 10080 == 0:
        return 'semana'
    if minutes and minutes % 1440 == 0:
        return f'{minutes // 1440} dias'
    return f'{minutes // 60}h' if minutes else 'crédito'


def bar(percent, width=20):
    filled = round(max(0.0, min(100.0, percent)) / 100 * width)
    return '█' * filled + '░' * (width - filled)


def resets_in(epoch, now=None):
    seconds = int(epoch - (time.time() if now is None else now))
    if seconds <= 0:
        return 'agora'
    days, rest = divmod(seconds, 86400)
    return f'{days}d{rest // 3600:02d}h' if days else f'{rest // 3600}h{rest % 3600 // 60:02d}m' if rest >= 3600 else f'{rest // 60}m'


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
    if not raw:
        path = (folder or Path('~/.claude').expanduser()) / '.credentials.json'
        raw = path.read_text() if path.is_file() else ''
    try:
        return (json.loads(raw).get('claudeAiOauth') or {}).get('accessToken') or None
    except ValueError:
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
            except ValueError:
                pass
        windows.append({'minutes': minutes, 'used': float(window['utilization']), 'resets': resets,
                        'label': 'opus' if key == 'seven_day_opus' else None})
    return windows


# ─── checagem: cache, paralelo, teto do roteador e alerta ─────────

CACHE_FILE = config_tool.CONFIG_DIR / 'usage-cache.json'
FRESH = {'claude': 120, 'codex': 300}  # seconds a reading stays good for the router


def thresholds():
    """routing.usage_skip_percent: the router skips an account at or above it (default 100).
    routing.usage_alert_percent: warn once per window when an account passes it (default 80)."""
    routing = config_tool.profiles().get('routing', {})
    return float(routing.get('usage_skip_percent', 100)), float(routing.get('usage_alert_percent', 80))


def load_cache():
    return config_tool.read_json(CACHE_FILE, {})


def store(key, reading):
    data = load_cache()
    data.setdefault('readings', {})[key] = dict(reading, at=int(time.time()))
    config_tool.write_json(CACHE_FILE, data)


def peak(windows):
    """Highest % among windows that have not reset yet."""
    now = time.time()
    return max((w['used'] for w in windows or [] if not w.get('resets') or w['resets'] > now), default=0.0)


def read(key, fetch, max_age=None):
    """Reading for an account: from the cache while fresh, otherwise fetched (and cached)."""
    tool = key.split(':')[0]
    cached = load_cache().get('readings', {}).get(key)
    if cached and time.time() - cached['at'] < (FRESH.get(tool, 120) if max_age is None else max_age):
        return cached
    reading = fetch()
    if reading is not None:
        store(key, reading)
    return reading


def alerts(key, reading, label):
    """Warning lines for windows past the alert threshold, each said once per reset."""
    _, alert = thresholds()
    data = load_cache()
    said = data.setdefault('alerted', {})
    lines = []
    for window in (reading or {}).get('windows', []):
        mark = f"{key}:{window['minutes']}:{window.get('label') or ''}:{int(window.get('resets') or 0)}"
        if window['used'] >= alert and mark not in said:
            said[mark] = int(time.time())
            reset = f", reseta em {resets_in(window['resets'])}" if window.get('resets') else ''
            lines.append(f"{label}: {window['used']:.0f}% da janela {window_label(window['minutes'], window.get('label'))} usada{reset}")
    if lines:
        config_tool.write_json(CACHE_FILE, data)
    return lines


def claude_reading(conf):
    windows = claude_limits(conf)
    return None if windows is None else {'windows': windows}


def codex_reading(env=None):
    status = codex_status(env)
    return None if status is None else status


def snapshot(claude_profiles, codex_accounts, openrouter_key):
    """Everything /usage shows, fetched at once: one thread per account."""
    from concurrent.futures import ThreadPoolExecutor
    jobs = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for name, conf in claude_profiles:
            jobs[('claude-who', name)] = pool.submit(claude_account, conf)
            jobs[('claude', name)] = pool.submit(claude_reading, conf)
        for name, env in codex_accounts:
            jobs[('codex', name)] = pool.submit(codex_reading, env)
        if openrouter_key:
            jobs[('openrouter', '')] = pool.submit(openrouter_credits, openrouter_key)
        results = {k: f.result() for k, f in jobs.items()}
    for (tool, name), value in results.items():
        if tool in ('claude', 'codex') and value is not None:
            store(f'{tool}:{name}', value)
    return results


def clock(epoch):
    """Reset time in local time: '14:00' today/tomorrow, 'seg 14:00' within a week."""
    moment = time.localtime(epoch)
    if epoch - time.time() < 86400:
        return time.strftime('%H:%M', moment)
    return ('seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom')[moment.tm_wday] + time.strftime(' %H:%M', moment)
