#!/usr/bin/env python3
"""Omni AI router: the `ai` command on macOS, Linux and Windows.

  ai                                  open the chat
  ai "question"                       automatic routing
  ai --claude|--codex|--cursor|--agy|--openrouter|--grok|--deepseek "question"
  ai --mode plan|edits|auto|yolo "question"
  ai --usage                         Uso e limites de todas as ferramentas e contas
  ai --setup                         First-run guide: logins and API keys
  ai --status | --reset | --log | --classify "prompt"
  ai --model ... | --account ... | --key ... | --sync ...
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from . import config as config_tool
from . import usage
from .osutils import REPO, enable_ansi, module_cmd, module_env, resolve_bin

STATE_FILE = Path(os.environ.get('SMART_AI_STATE', '~/.cache/smart-ai/state.json')).expanduser()
LOG_FILE = Path(os.environ.get('SMART_AI_LOG', '~/.cache/smart-ai/usage.log')).expanduser()
BINS = {tool: os.environ.get(tool.upper() + '_BIN', tool) for tool in ('claude', 'codex', 'agy', 'grok')}
BINS['cursor'] = os.environ.get('CURSOR_BIN', 'cursor-agent')
MODES = ('default', 'plan', 'edits', 'auto', 'yolo')
RATE_LIMITED = 2
NOT_READY = 3  # account without login: skip it, don't stop the fallback
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
    data = state()
    data[key] = value
    config_tool.write_json(STATE_FILE, data)


def log_usage(tool, profile, status):
    # Metadata only: the prompt never goes to the log
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, 'a') as stream:
        stream.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] tool={tool} profile={profile} status={status}\n")


def cooldown_left(profile):
    last = state().get(f'{profile}_last_rate_limit')
    if not last:
        return 0
    left = int(last) + int(profile_conf(profile).get('cooldown_minutes', 60)) * 60 - int(time.time())
    if left <= 0:
        set_state(f'{profile}_last_rate_limit', '')
        return 0
    return left


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


def run(argv, env=None, stdin=None):
    """Run a CLI, stream its output live, and return (exit code, captured text)."""
    executable = resolve_bin(argv[0])
    if not executable:
        say(f'{RED}✗ {argv[0]} não encontrado.{NC}')
        return 127, ''
    process = subprocess.Popen([executable] + argv[1:], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                               text=True, encoding='utf-8', errors='replace')
    if stdin is not None:
        process.stdin.write(stdin)
        process.stdin.close()
    captured = []
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    return process.wait(), ''.join(captured)


def installed(tool):
    return resolve_bin(BINS[tool]) is not None


def header(label, note):
    say(f'{label} {DIM}({note}){NC}')
    say(f'{DIM}─────────────────────────────────────{NC}')


# ─── executors ───────────────────────────────────────────────────

def run_claude(profile, prompt):
    if not installed('claude'):
        say(f'{RED}✗ Claude Code não instalado.{NC}')
        say(f'{DIM}  Execute o instalador: {REPO / "scripts" / ("install.ps1" if os.name == "nt" else "install.sh")}{NC}')
        return 1
    env = dict(os.environ)
    env.pop('CLAUDE_CONFIG_DIR', None)
    main_creds = Path('~/.claude/.credentials.json').expanduser()
    backup = None
    if profile == 'api-key' or profile.startswith('api-key:'):
        wanted = profile.partition(':')[2]
        env['ANTHROPIC_API_KEY'] = next((v for n, v, _ in config_tool.stored_keys('claude') if not wanted or n == wanted),
                                        config_tool.stored_key('claude')[0])
    else:
        conf = profile_conf(profile)
        config_dir = conf.get('config_dir')
        if config_dir:
            folder = Path(config_dir).expanduser()
            if not folder.is_dir():
                say(f"{RED}✗ Conta '{profile}' sem login.{NC}")
                say(f'{DIM}  Execute: ai --account login {profile}{NC}')
                return NOT_READY
            env['CLAUDE_CONFIG_DIR'] = str(folder)
        elif conf.get('credentials_file'):
            # Legacy profiles: swap the credentials file for this call only
            creds = Path(conf['credentials_file']).expanduser()
            if creds != main_creds:
                if not creds.is_file():
                    say(f'{RED}✗ Arquivo de credenciais não encontrado: {creds}{NC}')
                    say(f'{DIM}  Execute: ai --account add {profile}{NC}')
                    return NOT_READY
                backup = main_creds.read_bytes() if main_creds.exists() else b''
                main_creds.write_bytes(creds.read_bytes())
                say(f'{DIM}  → Credenciais trocadas para perfil: {profile}{NC}')
    emit_provider('claude', profile)
    header(f'{BLUE}🤖 Claude Code{NC}', f'perfil: {profile}')
    argv = [BINS['claude'], '--model', models()['claude']]
    argv += {'plan': ['--permission-mode', 'plan'], 'edits': ['--permission-mode', 'acceptEdits'],
             'auto': ['--permission-mode', 'auto'], 'yolo': ['--permission-mode', 'bypassPermissions']}.get(mode(), [])
    argv += ['-p', prompt, '--verbose']
    if os.environ.get('SMART_AI_EVENTS') == '1':
        argv += ['--output-format', 'stream-json']
    try:
        code, output = run(argv, env)
    finally:
        if backup is not None:
            main_creds.write_bytes(backup)
    if hit_limit(output, code):
        remember_limit(f'claude:{profile}', output)
        mark_rate_limited(profile)
        log_usage('claude', profile, 'rate_limited')
        return RATE_LIMITED
    if code == 0:
        remember_limit(f'claude:{profile}', '')
    log_usage('claude', profile, 'success' if code == 0 else f'error:{code}')
    return code


LIMIT_MESSAGE = re.compile(r"[^\n]*(?:hit your (?:usage|session) limit|quota reached|rate limit|try again at|resets? (?:at|in))[^\n]*", re.I)


LIMIT_IN = re.compile(r'(?:resets?|try again)\s+in\s+((?:\d+\s*[dhms]\s*)+)', re.I)
LIMIT_AT = re.compile(r'(?:resets?(?:\s+at)?|try again at)\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\b', re.I)
LIMIT_FALLBACK = 24 * 3600  # no reset time in the message: stop showing it after a day


def limit_until(text, at):
    """When the tool said the limit ends, as epoch seconds; None when the message gives no time."""
    found = LIMIT_IN.search(text)
    if found:
        units = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
        return at + sum(int(n) * units[u.lower()] for n, u in re.findall(r'(\d+)\s*([dhms])', found.group(1), re.I))
    found = LIMIT_AT.search(text)
    if not found:
        return None
    hour, minute, half = int(found.group(1)), int(found.group(2) or 0), (found.group(3) or '').lower()
    if half == 'pm' and hour < 12:
        hour += 12
    elif half == 'am' and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    seen = time.localtime(at)
    until = time.mktime((seen.tm_year, seen.tm_mon, seen.tm_mday, hour, minute, 0, 0, 0, -1))
    return int(until if until > at else until + 86400)


def remember_limit(key, output):
    """Keep the tool's own words about its limit (e.g. 'resets 7:40pm') for /usage."""
    match = LIMIT_MESSAGE.search(re.sub(r'\\u2019', "'", output))
    data = state()
    limits = data.setdefault('limits', {})
    if match:
        text = match.group(0)
        inner = re.search(r'"message"\s*:\s*"((?:[^"\\\\]|\\\\.)*)"', text)  # JSON error: keep only its message
        if inner:
            text = inner.group(1)
        text = ' '.join(text.split())[:220]
        now = int(time.time())
        limits[key] = {'text': text, 'at': now, 'until': limit_until(text, now)}
    else:
        limits.pop(key, None)
    config_tool.write_json(STATE_FILE, data)


def active_limit(item, now=None):
    """The stored limit if it still holds; expired ones (reset time passed) count as gone."""
    if not item:
        return None
    now = time.time() if now is None else now
    until = item.get('until') or limit_until(item['text'], item['at']) or item['at'] + LIMIT_FALLBACK
    return dict(item, until=until) if now < until else None


def hit_limit(output, code):
    """Only Claude's own verdict counts: the final error result, a rejected rate-limit event, or its
    plain error lines. Tool output (files, PR text, command logs) is ignored, so a review that mentions
    "rate limit" can't put an account in cooldown."""
    verdicts = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            verdicts.append(line)  # Claude's stderr/plain error lines
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'result' and event.get('is_error'):
            verdicts.append(str(event.get('result', '')))
        elif event.get('type') == 'rate_limit_event' and \
                (event.get('rate_limit_info') or {}).get('status') in ('rejected', 'exceeded'):
            return True
    if os.environ.get('SMART_AI_EVENTS') != '1':
        # Text mode: only the tail of a failed run is Claude talking about itself
        verdicts = output.splitlines()[-5:] if code != 0 else []
    return any(LIMIT_TEXT.search(v) for v in verdicts)


def over_ceiling(key, fetch, label):
    """Real % of an account before using it: warns past the alert line, and True when it is at or above
    the router's ceiling (routing.usage_skip_percent), so the account is skipped instead of failing."""
    reading = usage.read(key, fetch)
    for line in usage.alerts(key, reading, label):
        say(f'{YELLOW}⚠ {line}{NC}')
    skip, _ = usage.thresholds()
    top = usage.peak((reading or {}).get('windows'))
    if reading and top >= skip:
        say(f'{DIM}  ⏭ {label}: {top:.0f}% usado, acima do teto de {skip:.0f}%; pulando{NC}')
        return True
    return False


def key_account(tool, name):
    """Limit/cooldown id of an API key: '<tool>:api-key' for the main one, '<tool>:api-key:<name>' for extras."""
    base = f'{tool}:api-key' if tool else 'api-key'
    return base if name in ('principal', 'env') else f'{base}:{name}'


def run_with_key_retry(tool, argv, env_var, label):
    """Main login first, then each extra account of the tool (/account add TOOL NAME), then the saved API key."""
    accounts = [('primary', {})] + [(name, config_tool.tool_env(tool, conf)) for name, conf in config_tool.tool_accounts(tool, True)]
    code = 1
    for name, env in accounts:
        account = tool if name == 'primary' else f'{tool}:{name}'
        if cooldown_left(account):
            say(f'{DIM}  ⏳ {label}: conta {name} em cooldown{NC}')
            continue
        if tool == 'codex' and over_ceiling(f'codex:{name}', lambda env=env: usage.codex_reading(env or None), f'{label} {name}'):
            continue
        if name != 'primary':
            say(f'{YELLOW}🔄 {label}: tentando a conta {name}{NC}')
        code, output = run(argv, dict(os.environ, **env) if env else None)
        remember_limit(account, output if code != 0 else '')
        if code == 0:
            break
        if LIMIT_TEXT.search(output[-3000:]):
            mark_rate_limited(account)
    for name, key, origin in config_tool.stored_keys(tool):
        account = key_account(tool, name)
        if code == 0 or origin == 'env' or cooldown_left(account):
            continue
        say(f'{YELLOW}🔑 {label}: tentando com a chave {name}{NC}')
        code, output = run(argv, dict(os.environ, **{env_var: key}))
        remember_limit(account, output if code != 0 else '')
        if code != 0 and LIMIT_TEXT.search(output[-3000:]):
            mark_rate_limited(account)
    log_usage(tool, 'N/A', 'success' if code == 0 else 'error')
    return code


def run_codex(prompt):
    emit_provider('codex')
    header(f'{GREEN}📦 Codex CLI{NC}', 'economizando Claude')
    values = models()
    argv = [BINS['codex'], 'exec', '--skip-git-repo-check', '-m', values['codex'],
            '-c', f'model_reasoning_effort="{values["codex_effort"]}"']
    if os.environ.get('SMART_AI_EVENTS') == '1':
        argv.append('--json')
    argv += {'plan': ['--sandbox', 'read-only'], 'edits': ['--sandbox', 'workspace-write'],
             'auto': ['--sandbox', 'workspace-write', '-c', 'approval_policy="never"'],
             'yolo': ['--dangerously-bypass-approvals-and-sandbox']}.get(mode(), [])
    return run_with_key_retry('codex', argv + ['--', prompt], 'CODEX_API_KEY', 'Codex')


def run_agy(prompt):
    emit_provider('agy')
    header(f'{MAGENTA}🚀 Antigravity{NC}', 'economizando Claude')
    argv = [BINS['agy']]
    if os.environ.get('SMART_AI_EVENTS') == '1':
        argv += ['--output-format', 'stream-json']
    if models()['agy']:
        argv += ['--model', models()['agy']]
    argv += {'plan': ['--mode', 'plan'], 'edits': ['--mode', 'accept-edits'], 'auto': ['--mode', 'auto'],
             'yolo': ['--dangerously-skip-permissions']}.get(mode(), [])
    return run_with_key_retry('agy', argv + ['-p', prompt], 'GEMINI_API_KEY', 'Antigravity')


def run_openrouter(prompt):
    emit_provider('openrouter')
    keys = config_tool.stored_keys('openrouter')
    if not keys:
        say(f'{RED}✗ OPENROUTER_API_KEY não configurada.{NC}')
        say(f'{DIM}  Execute: ai --key openrouter (ou /key openrouter no chat){NC}')
        log_usage('openrouter', 'N/A', 'no_api_key')
        return 1
    values = models()
    paid = values['openrouter_paid'] == 'on'
    header(f'{YELLOW}🌐 OpenRouter{NC}', ('modelos pagos autorizados' if paid else 'modelo gratuito') + ' + PII masking')
    argv = module_cmd('openrouter')
    if values['openrouter']:
        argv += ['--model', values['openrouter']]
    code = 1
    for i, (name, key, _) in enumerate(keys):
        if i:
            say(f'{YELLOW}🔑 OpenRouter: tentando com a chave {name}{NC}')
        env = module_env(dict(os.environ, OPENROUTER_API_KEY=key, SMART_AI_OPENROUTER_PAID=values['openrouter_paid']))
        code, _ = run(argv, env, stdin=prompt)
        if code == 0:
            break
    log_usage('openrouter', 'N/A', 'success' if code == 0 else 'error')
    return code


def run_cursor(prompt):
    emit_provider('cursor')
    header(f'{DIM}▲ Cursor CLI{NC}', 'economizando Claude')
    if not installed('cursor'):
        say(f'{RED}✗ Cursor CLI não instalado.{NC}')
        say(f'{DIM}  Instale: curl https://cursor.com/install -fsS | bash  e rode: cursor-agent login{NC}')
        log_usage('cursor', 'N/A', 'not_installed')
        return 1
    argv = [BINS['cursor'], '-p', '--trust']
    if os.environ.get('SMART_AI_EVENTS') == '1':
        argv += ['--output-format', 'stream-json']  # same events as Claude Code's stream-json
    if models()['cursor']:
        argv += ['--model', models()['cursor']]
    # Without --force the headless agent only proposes changes
    argv += {'plan': ['--mode', 'plan'], 'edits': ['--force'], 'auto': ['--force'], 'yolo': ['--force']}.get(mode(), [])
    # Subscription login first, then each saved CURSOR_API_KEY
    return run_with_key_retry('cursor', argv + [prompt], 'CURSOR_API_KEY', 'Cursor')


def run_deepseek(prompt):
    emit_provider('deepseek')
    keys = config_tool.stored_keys('deepseek')
    if not keys:
        say(f'{RED}✗ DEEPSEEK_API_KEY não configurada.{NC}')
        say(f'{DIM}  Execute: ai --key deepseek (ou /key deepseek no chat){NC}')
        log_usage('deepseek', 'N/A', 'no_api_key')
        return 1
    header(f'{BLUE}◆ DeepSeek{NC}', 'API paga por uso + PII masking')
    argv = module_cmd('deepseek') + (['--model', models()['deepseek']] if models()['deepseek'] else [])
    code = 1
    for i, (name, key, _) in enumerate(keys):
        if i:
            say(f'{YELLOW}🔑 DeepSeek: tentando com a chave {name}{NC}')
        code, _ = run(argv, module_env(dict(os.environ, DEEPSEEK_API_KEY=key)), stdin=prompt)
        if code == 0:
            break
    log_usage('deepseek', 'N/A', 'success' if code == 0 else 'error')
    return code


def run_grok(prompt):
    emit_provider('grok')
    model = models()['grok']
    if installed('grok'):
        # Grok Build: SuperGrok/X Premium+ login first, the saved xAI key as fallback
        header(f'{BLUE}✦ Grok Build{NC}', 'economizando Claude')
        argv = [BINS['grok'], '--no-auto-update', '--cwd', os.getcwd()]
        if os.environ.get('SMART_AI_EVENTS') == '1':
            argv += ['--output-format', 'streaming-messages-json']  # same events as Claude Code's stream-json
        if model:
            argv += ['-m', model]
        # Headless 'plan' is only a compatibility alias; dontAsk keeps it to read-only tools
        argv += {'plan': ['--permission-mode', 'dontAsk'], 'edits': ['--permission-mode', 'acceptEdits'],
                 'auto': ['--permission-mode', 'auto'], 'yolo': ['--permission-mode', 'bypassPermissions']}.get(mode(), [])
        return run_with_key_retry('grok', argv + ['-p', prompt], 'XAI_API_KEY', 'Grok')
    # No CLI: plain chat over the API, paid per use
    keys = config_tool.stored_keys('grok')
    if not keys:
        say(f'{RED}✗ Grok Build não instalado e sem chave da xAI.{NC}')
        say(f'{DIM}  Instale: curl -fsSL https://x.ai/cli/install.sh | bash  e rode: grok login{NC}')
        say(f'{DIM}  Ou use uma chave: ai --key grok (ou /key grok no chat){NC}')
        log_usage('grok', 'N/A', 'no_api_key')
        return 1
    header(f'{BLUE}✦ Grok{NC}', 'API da xAI, pago por uso + PII masking')
    argv = module_cmd('grok') + (['--model', model] if model else [])
    code = 1
    for i, (name, key, _) in enumerate(keys):
        if i:
            say(f'{YELLOW}🔑 Grok: tentando com a chave {name}{NC}')
        code, _ = run(argv, module_env(dict(os.environ, XAI_API_KEY=key)), stdin=prompt)
        if code == 0:
            break
    log_usage('grok', 'N/A', 'success' if code == 0 else 'error')
    return code


# ─── routing ─────────────────────────────────────────────────────

# Groq retires models often; the second one covers the first going away
GROQ_CLASSIFIER_MODELS = [('qwen/qwen3.8-27b', 'none'), ('openai/gpt-oss-20b', 'low')]
GROK_CLASSIFIER_MODEL = 'grok-4-fast-non-reasoning'
# Cerebras: free tier and very fast; the second free classifier when Groq is down or out of quota
CEREBRAS_CLASSIFIER_MODELS = ['llama3.1-8b', 'gpt-oss-120b']
CLASSIFIER_MODELS = ['nvidia/nemotron-3.5-lightning:free', 'google/gemma-4-26b-a4b-it:free', 'openrouter/free']
CLASSIFIER_SYSTEM = (
    'You are a router, not an assistant: never answer or carry out the prompt, only label it.\n'
    'The CLI runs inside the user\'s project directory.\n'
    'complex: asks to analyze, change, create, fix, improve, run or test something (code, files, repository, '
    'shell, GitHub), even without naming a file, or needs real coding work.\n'
    'simple: a general question a plain chat model answers without seeing any file '
    '(concepts, definitions, translations, trivia, short text).\n'
    'Reply with exactly one word: complex or simple.')


def ask_classifier(url, key, data, labels=('complex', 'simple')):
    """One labelling request to an OpenAI-compatible endpoint; None on any failure."""
    import urllib.request
    request = urllib.request.Request(url, data=json.dumps(data).encode(), method='POST', headers={
        'Content-Type': 'application/json', 'Authorization': f'Bearer {key}', 'User-Agent': 'omni-ai-cli'})
    try:
        with urllib.request.urlopen(request, timeout=6) as resp:
            answer = (json.loads(resp.read().decode())['choices'][0]['message']['content'] or '').lower()
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    return next((label for label in labels if label in answer), None)


def free_classifiers():
    """(provider, url, request extras) of the free labelling backends, in the order they are tried."""
    groq = 'https://api.groq.com/openai/v1/chat/completions'
    return [('groq', groq, {'model': model, 'reasoning_effort': effort}) for model, effort in GROQ_CLASSIFIER_MODELS] + [
        ('cerebras', 'https://api.cerebras.ai/v1/chat/completions', {'model': model}) for model in CEREBRAS_CLASSIFIER_MODELS] + [
        # Free OpenRouter models fail now and then (rate limit, empty reply); the second try covers most of it
        ('openrouter', 'https://openrouter.ai/api/v1/chat/completions',
         {'models': CLASSIFIER_MODELS, 'reasoning': {'enabled': False},
          'provider': {'max_price': {'prompt': 0, 'completion': 0}}}),
    ] * 2


def classify_llm(prompt):
    """Groq first (large free quota), then Cerebras, then free OpenRouter, then paid Grok; None when none answers."""
    from .pii import PIIMasker
    masked, _ = PIIMasker().mask(prompt)
    messages = [{'role': 'system', 'content': CLASSIFIER_SYSTEM},
                {'role': 'user', 'content': f'<prompt>\n{masked[:4000]}\n</prompt>\nLabel (complex or simple):'}]
    backends = free_classifiers() + [
        # Paid: only reached when every free service fails
        ('grok', 'https://api.x.ai/v1/chat/completions', {'model': models()['grok'] or GROK_CLASSIFIER_MODEL}),
    ]
    for provider, url, extra in backends:
        for _, key, _ in config_tool.stored_keys(provider):
            # Room for a short reasoning pass on models that can't switch it off
            label = ask_classifier(url, key, dict(extra, messages=messages, temperature=0, max_tokens=300))
            if label:
                return label
    return None


def classify(prompt):
    prompt = os.environ.get('SMART_AI_CLASSIFY_PROMPT') or prompt
    return classify_llm(prompt) or classify_rules(prompt)


def classify_rules(prompt):
    lower = prompt.lower()
    routing = config().get('routing', {})
    if any(k in lower for k in routing.get('complex_keywords', [])):
        return 'complex'
    # Anything that acts on the workspace needs an agent with file access; OpenRouter only chats
    if re.search(r'\b(projeto|project|repo|reposit[oó]rio|codebase|c[oó]digo|code|arquivos?|files?|pastas?|folder|'
                 r'github|git|commit|push|pull request|pr|readme|about|branch|analis|analy[sz]|atuali[sz]|update|'
                 r'edit|alter|mud[ae]|ajust|adicion|add|remov|delet|instal|rod[ae]|run|execut|test)', lower):
        return 'complex'
    if re.search(r'\.(py|js|ts|go|rs|java|kt|dart|swift|rb|sh|yml|yaml|json|toml|sql)', prompt):
        return 'complex'
    if re.search(r'(/|\\|~/|\./)[a-zA-Z]', prompt):
        return 'complex'
    if any(k in lower for k in routing.get('simple_keywords', [])):
        return 'simple'
    if len(prompt) <= int(routing.get('max_simple_length', 200)) and \
            not re.search(r'function|class|module|package|dependency|error|stack|trace|test|spec', lower):
        return 'simple'
    return 'complex'


def claude_with_fallback(prompt):
    for profile in config_tool.enabled_profiles():
        left = cooldown_left(profile)
        if left:
            say(f"{DIM}  ⏳ Perfil '{profile}' em cooldown (restam: {left // 60}m{left % 60}s){NC}")
            continue
        conf = config_tool.profiles()['profiles'].get(profile, {})
        if over_ceiling(f'claude:{profile}', lambda conf=conf: usage.claude_reading(conf), f'Claude {profile}'):
            continue
        result = run_claude(profile, prompt)
        if result == 0:
            return 0
        if result not in (RATE_LIMITED, NOT_READY):
            return result
        say(f'{YELLOW}🔄 Tentando próximo perfil...{NC}')
    for name, _, _ in config_tool.stored_keys('claude'):
        account = key_account('', name)
        if cooldown_left(account):
            continue
        say(f'{YELLOW}🔑 Claude: tentando com a chave {name}{NC}')
        result = run_claude(account, prompt)
        if result == 0:
            return 0
        if result != RATE_LIMITED:
            return result
    say(f'\n{RED}✗ Todas as contas Claude estão em rate limit!{NC}')
    say(f'{YELLOW}  Tentando resolver com ferramentas alternativas...{NC}\n')
    for tool, runner in (('codex', run_codex), ('agy', run_agy)):
        if installed(tool):
            say(f'{YELLOW}↪ Forçando {tool} como último recurso...{NC}')
            if runner(prompt) == 0:
                return 0
    say(f'\n{RED}✗ Nenhuma ferramenta disponível. Aguarde o cooldown.{NC}')
    return 1


def route(prompt, tool='auto'):
    forced = {'claude': claude_with_fallback, 'codex': run_codex, 'agy': run_agy, 'openrouter': run_openrouter, 'grok': run_grok,
              'deepseek': run_deepseek, 'cursor': run_cursor}
    if tool in forced:
        return forced[tool](prompt)
    if classify(prompt) == 'complex':
        say(f'{DIM}📊 Classificado como: {CYAN}query complexa{NC}{DIM} → usando Claude{NC}\n')
        return claude_with_fallback(prompt)
    say(f'{DIM}📊 Classificado como: {YELLOW}query simples{NC}{DIM} → roteando para alternativa{NC}\n')
    routing = config().get('routing', {})
    order = [routing.get('prefer_tool_for_simple', 'codex')] + routing.get('fallback_order', ['openrouter', 'codex', 'agy'])
    # Grok, DeepSeek and Cursor cost money, so auto mode only uses them when listed in routing.fallback_order
    for name in dict.fromkeys(t for t in order if t in forced and t != 'claude'):
        if name == 'grok' and not (installed('grok') or config_tool.stored_key('grok')[0]):
            continue
        if name == 'deepseek' and not config_tool.stored_key('deepseek')[0]:
            continue
        if name in ('codex', 'agy', 'cursor') and not installed(name):
            continue
        if forced[name](prompt) == 0:
            return 0
    say(f'\n{DIM}→ Usando Claude como fallback final{NC}')
    return claude_with_fallback(prompt)


# ─── management commands ─────────────────────────────────────────

def version(tool):
    try:
        out = subprocess.run([resolve_bin(BINS[tool]), '--version'], capture_output=True, text=True, timeout=15).stdout
        return out.strip().splitlines()[0] if out.strip() else '?'
    except (OSError, subprocess.TimeoutExpired, TypeError):
        return '?'


def show_status():
    say(f'{BOLD}{CYAN}  🧠 Omni AI CLI{NC}\n')
    say(f'{BOLD}  Perfis Claude:{NC}\n')
    for name, conf in config_tool.ordered(config_tool.profiles()):
        left = cooldown_left(name)
        if not conf.get('enabled', True):
            icon, text = f'{RED}○{NC}', 'desabilitado'
        elif left:
            icon, text = f'{YELLOW}◐{NC}', f'cooldown (restam: {left // 60}m{left % 60}s)'
        else:
            icon, text = f'{GREEN}●{NC}', 'disponível'
        say(f"  {icon} {BOLD}{name}{NC}: {conf.get('name', '')}")
        say(f'    Status: {text}')
        if conf.get('config_dir'):
            say(f"    Login: {GREEN}pasta isolada{NC} {DIM}{conf['config_dir']}{NC}")
        elif conf.get('credentials_file'):
            ok = Path(conf['credentials_file']).expanduser().is_file()
            say('    Credenciais: ' + (f'{GREEN}✓ configurado{NC}' if ok else f'{RED}✗ arquivo não encontrado{NC}'))
        say()
    keys = [p for p in config_tool.KEYS if config_tool.stored_key(p)[0]]
    say(f"  Chaves de API: {', '.join(keys) if keys else 'nenhuma (ai --key)'}")
    say(f'  Modo padrão: {mode()}\n')
    say(f'{BOLD}  Ferramentas:{NC}\n')
    for tool, label in (('claude', 'Claude Code'), ('codex', 'Codex CLI'), ('agy', 'Antigravity'), ('grok', 'Grok Build'), ('cursor', 'Cursor CLI')):
        say(f'  {GREEN}●{NC} {label}: {version(tool)}' if installed(tool) else f'  {RED}○{NC} {label}: não instalado')
    if LOG_FILE.is_file():
        lines = LOG_FILE.read_text(errors='replace').splitlines()
        counts = {t: sum(f'tool={t} ' in l for l in lines) for t in ('claude', 'codex', 'agy', 'openrouter', 'grok', 'deepseek', 'cursor')}
        say(f'\n{BOLD}  📊 Estatísticas de uso:{NC}')
        say(f'    Total de chamadas: {len(lines)}')
        say('    ' + ' | '.join(f'{t}: {n}' for t, n in counts.items()))
    say()


def ago(seconds):
    seconds = int(seconds)
    return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m' if seconds >= 3600 else f'{seconds // 60}m'


def show_usage(watch=False):
    """One screen for every tool and account: who it is, % used of each window (when the tool says),
    or the tool's own limit message. With watch, redraws every 30s until Ctrl-C."""
    if not watch:
        return render_usage()
    try:
        while True:
            sys.stdout.write('\033[H\033[2J')
            render_usage()
            say(f"\n{DIM}atualiza a cada 30s · {time.strftime('%H:%M:%S')} · Ctrl-C sai{NC}")
            time.sleep(30)
    except KeyboardInterrupt:
        say()
    return 0


def render_usage():
    limits = state().get('limits', {})
    today = time.strftime('%Y-%m-%d')
    lines = LOG_FILE.read_text(errors='replace').splitlines() if LOG_FILE.is_file() else []
    skip, alert = usage.thresholds()

    def calls(tool):
        return sum(1 for l in lines if l.startswith('[' + today) and f'tool={tool} ' in l)

    def limit_line(key):
        item = active_limit(limits.get(key))
        if not item:
            return ''
        back = f" · volta em {ago(item['until'] - time.time())}" if limit_until(item['text'], item['at']) else ''
        return f"{YELLOW}limitado{NC} {DIM}· {item['text']} (visto há {ago(time.time() - item['at'])}{back}){NC}"

    def who(info):
        return f" {DIM}· {info['email']}{' · ' + info['plan'] if info.get('plan') else ''}{NC}" if info and info.get('email') else ''

    def windows(items, pad=15):
        for window in items:
            used = window['used']
            color = RED if used >= max(alert + 10, 90) else YELLOW if used >= alert - 10 else GREEN
            reset = ''
            if window.get('resets'):
                reset = f"  {DIM}reseta em {usage.resets_in(window['resets'])} · {usage.clock(window['resets'])}{NC}"
            name = usage.window_label(window['minutes'], window.get('label'))
            filled = usage.bar(used, 24).count('█')
            say(f"{' ' * pad}{DIM}{name:<7}{NC} {color}{'█' * filled}{NC}{DIM}{'░' * (24 - filled)}{NC} {BOLD}{color}{used:>3.0f}%{NC}{reset}")

    def keys_of(tool, prefix):
        for name, value, origin in config_tool.stored_keys(tool):
            account = key_account(prefix, name)
            text = limit_line(account if prefix else f'claude:{account}')
            left = cooldown_left(account)
            status = (text or f'{YELLOW}limitada{NC}') + f' {DIM}· volta em {left // 60}m{NC}' if left else text or f'{GREEN}configurada{NC}'
            say(f"  {'chave ' + name:<12} {status} {DIM}· {value[:7]}…{value[-4:]}{' · variável de ambiente' if origin == 'env' else ''}{NC}")

    def state_of(live, fallback_key, cooldown=0):
        if live and live.get('windows'):
            top = usage.peak(live['windows'])
            if live.get('blocked') or top >= 100:
                return f'{RED}no limite{NC}'
            note = f" {DIM}(última leitura há {ago(time.time() - live['at'])}; a Anthropic pediu uma pausa nas consultas){NC}" \
                if live.get('stale') else ''
            if top >= skip:
                return f'{YELLOW}acima do teto de {skip:.0f}% · o Omni pula{NC}' + note
            return f'{GREEN}disponível{NC}' + note
        text = limit_line(fallback_key)
        if not text and not cooldown and fallback_key.startswith('claude:') and usage.load_cache().get('backoff', {}).get(fallback_key, 0) > time.time():
            text = f"{GREEN}disponível{NC} {DIM}· uso indisponível agora: a Anthropic pediu uma pausa nas consultas{NC}"
        if cooldown:
            return (text or f'{YELLOW}limitado{NC}') + f' {DIM}· o Omni tenta de novo em {cooldown // 60}m{NC}'
        return text or f'{GREEN}sem limite registrado{NC}'

    profiles = [(n, c) for n, c in config_tool.ordered(config_tool.profiles()) if c.get('enabled', True)]
    codex = [('primary', None)] + [(n, config_tool.tool_env('codex', c)) for n, c in config_tool.tool_accounts('codex', True)] \
        if installed('codex') else []
    or_keys = config_tool.stored_keys('openrouter')
    if sys.stdout.isatty():
        sys.stdout.write(f'{DIM}consultando contas…{NC}\r')
        sys.stdout.flush()
    data = usage.snapshot(profiles, codex, or_keys)
    if sys.stdout.isatty():
        sys.stdout.write('\033[K')
    say(f'{BOLD}Uso e limites{NC}  {DIM}% usado de cada janela · teto do roteador {skip:.0f}% · alerta em {alert:.0f}%{NC}\n')

    say(f'{BOLD}Claude{NC}  {DIM}{calls("claude")} chamadas hoje{NC}')
    for name, conf in config_tool.ordered(config_tool.profiles()):
        if not conf.get('enabled', True):
            say(f'  {name:<12} {DIM}desativada{NC}')
            continue
        info = data.get(('claude-who', name))
        if not info and config_tool.logged_in(conf) is False:
            say(f'  {name:<12} {RED}sem login{NC} {DIM}· /account login {name}{NC}')
            continue
        live = data.get(('claude', name))
        say(f'  {name:<12} {state_of(live, f"claude:{name}", cooldown_left(name))}{who(info)}')
        windows((live or {}).get('windows', []))
    keys_of('claude', '')
    say(f'  {DIM}+ nova conta: /account add NOME · outra chave: /key add claude [NOME]{NC}')
    for tool, label in (('codex', 'Codex'), ('agy', 'Antigravity'), ('grok', 'Grok Build'), ('cursor', 'Cursor')):
        say(f'\n{BOLD}{label}{NC}  {DIM}{calls(tool)} chamadas hoje{NC}')
        if not installed(tool):
            say(f'  {RED}não instalado{NC}')
            keys_of(tool, tool)
            say(f"  {DIM}+ usar com chave de API: /key {'add ' if config_tool.stored_keys(tool) else ''}{tool}{NC}")
            continue
        for name, conf in [('primary', None)] + (config_tool.tool_accounts(tool, True) if tool in config_tool.TOOL_HOMES else []):
            account = tool if conf is None else f'{tool}:{name}'
            live = data.get(('codex', name)) if tool == 'codex' else None
            say(f"  {'assinatura' if conf is None else name:<12} {state_of(live, account, cooldown_left(account))}{who(live)}")
            windows((live or {}).get('windows', []))
        keys_of(tool, tool)
        key_hint = f'outra chave: /key add {tool} [NOME]' if config_tool.stored_keys(tool) else f'chave de reserva: /key {tool}'
        say(f'  {DIM}+ nova conta: /account add {tool} NOME · {key_hint}{NC}')
    say(f'\n{BOLD}OpenRouter{NC}  {DIM}{calls("openrouter")} chamadas hoje{NC}')
    if not or_keys:
        say(f'  {DIM}sem chave · /key openrouter{NC}')
    for name, value, _ in or_keys:
        credits = data.get(('openrouter', name))
        label = f"chave {name}"
        if credits and credits['limit']:
            say(f"  {label:<12} {GREEN}configurada{NC} {DIM}· US$ {credits['used']:.2f} de US$ {float(credits['limit']):.2f}{NC}")
            windows([{'minutes': 0, 'used': credits['used'] / float(credits['limit']) * 100}])
        elif credits:
            say(f"  {label:<12} {GREEN}configurada{NC} {DIM}· US$ {credits['used']:.2f} usados · sem teto{NC}")
        else:
            say(f"  {label:<12} {GREEN}configurada{NC}")
    say(f"  {DIM}+ outra chave: /key add openrouter [NOME]{NC}")
    say(f'\n{DIM}Teto e alerta: routing.usage_skip_percent e routing.usage_alert_percent no profiles.json · /usage watch{NC}')
    return 0


def reset_cooldowns():
    data = state()
    for key in list(data):
        if key.endswith('_last_rate_limit'):
            data[key] = ''
    config_tool.write_json(STATE_FILE, data)
    say(f'{GREEN}✓ Cooldowns resetados para todos os perfis.{NC}')


def show_log():
    if not LOG_FILE.is_file():
        say(f'{DIM}Nenhum log encontrado ainda.{NC}')
        return
    say(f'{BOLD}Últimas 20 entradas do log:{NC}\n')
    say('\n'.join(LOG_FILE.read_text(errors='replace').splitlines()[-20:]))


def update():
    """Pull the latest version into the install folder; settings live in ~/.config and are kept."""
    if (REPO / '.git').is_dir() and resolve_bin('git'):
        code = subprocess.call(['git', '-C', str(REPO), 'pull', '--ff-only'])
        say(f'{GREEN}✓ Omni AI atualizado.{NC}' if code == 0 else f'{RED}✗ Não consegui atualizar; veja a mensagem acima.{NC}')
        return code
    if os.environ.get('SMART_AI_INSTALLER') == 'npm':
        command = 'npm install -g omni-ai-cli@latest'
        say(f'Atualizando com: {command}')
        npm = resolve_bin('npm')
        return subprocess.call([npm, 'install', '-g', 'omni-ai-cli@latest']) if npm else 1
    if os.name == 'nt':
        command = 'irm https://raw.githubusercontent.com/samuelrms/omni-ai-cli/main/get.ps1 | iex'
    else:
        command = 'curl -fsSL https://raw.githubusercontent.com/samuelrms/omni-ai-cli/main/get.sh | bash'
    say(f'Instalação sem git. Para atualizar, rode:\n  {command}')
    return 1


USAGE = f'''{BOLD}Uso:{NC}
  ai                                 Abrir o chat (/help dentro dele)
  ai "pergunta"                      Roteamento automático
  ai --claude|--codex|--cursor|--agy|--openrouter|--grok|--deepseek "pergunta"
  ai --mode plan|edits|auto|yolo "pergunta"
  ai --model [chave valor]           Modelos
  ai --account [list|add|login|sync|remove] NOME
  ai --key [claude|codex|cursor|agy|openrouter|grok|deepseek|groq|cerebras|remove X]
  ai --sync [--dry-run]              MCPs e skills entre Claude, Codex e agy
  ai --usage                         Uso e limites de todas as ferramentas e contas
  ai --setup                          Guia de primeiro acesso: logins e chaves
  ai --status | --reset | --log | --classify "prompt"
  ai --update                        Atualizar para a versão mais nova
'''


def main(argv):
    enable_ansi()
    if not argv and sys.stdin.isatty():
        from . import chat
        return chat.main([])
    head = argv[0] if argv else ''
    delegated = {'--model': ('config_tool', ['model']), '-m': ('config_tool', ['model']),
                 '--account': ('config_tool', ['account']), '--key': ('config_tool', ['key']),
                 '--keys': ('config_tool', ['key']), '--setup-openrouter': ('config_tool', ['key', 'set', 'openrouter']),
                 '--setup-secondary': ('config_tool', ['account', 'add', 'secondary'])}
    if head in ('--chat', '--interactive', '-i', '--resume', '--sessions'):
        from . import chat
        return chat.main(argv[1:] if head in ('--chat', '--interactive', '-i') else argv)
    if head in delegated:
        _, base = delegated[head]
        try:
            config_tool.main(base + argv[1:])
        except SystemExit as error:
            if error.code not in (None, 0):
                print(error.code, file=sys.stderr)
                return 1
        return 0
    if head in ('--update', '--upgrade'):
        return update()
    if head == '--sync':
        from . import sync
        return sync.main(argv[1:])
    tool, words, i = 'auto', [], 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--':
            words += argv[i + 1:]
            break
        if arg in ('--claude', '-c', '--codex', '-x', '--agy', '-a', '--openrouter', '-o', '--grok', '-g', '--deepseek', '-d', '--cursor', '-u'):
            tool = {'c': 'claude', 'x': 'codex', 'a': 'agy', 'o': 'openrouter', 'g': 'grok', 'd': 'deepseek', 'u': 'cursor'}.get(arg.lstrip('-'), arg.lstrip('-'))
        elif arg == '--mode':
            value = argv[i + 1] if i + 1 < len(argv) else ''
            if value not in MODES:
                print('Modo inválido: use default, plan, edits, auto ou yolo', file=sys.stderr)
                return 1
            os.environ['SMART_AI_MODE'] = value
            i += 1
        elif arg in ('--status', '-s'):
            show_status()
            return 0
        elif arg == '--setup':
            from . import onboarding
            onboarding.main()
            return 0
        elif arg in ('--usage', '--limits'):
            return show_usage(watch='watch' in argv[i + 1:] or '--watch' in argv[i + 1:])
        elif arg in ('--reset', '-r'):
            reset_cooldowns()
            return 0
        elif arg in ('--log', '-l'):
            show_log()
            return 0
        elif arg == '--classify':
            prompt = ' '.join(argv[i + 1:])
            result = classify(prompt)
            say(f'Prompt: {DIM}"{prompt}"{NC}')
            say('Classificação: ' + (f'{GREEN}SIMPLES{NC} → alternativas' if result == 'simple' else f'{CYAN}COMPLEXA{NC} → Claude'))
            return 0
        elif arg in ('--help', '-h'):
            say(USAGE)
            return 0
        else:
            words.append(arg)
        i += 1
    prompt = ' '.join(words).strip()
    if not prompt:
        say(USAGE)
        return 1
    return route(prompt, tool)


def entry():
    try:
        return main(sys.argv[1:])
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Output piped into something that stopped reading (e.g. `ai --usage | head`)
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == '__main__':
    sys.exit(entry())
