"""Per-tool executors: run each CLI/API, with account + API-key fallback and rate-limit handling."""
import os
from pathlib import Path
import signal
import threading

from .. import config as config_tool
from .. import usage
from ..osutils import REPO, module_cmd, module_env
from .base import (
    BINS, NOT_READY, RATE_LIMITED, RED, GREEN, YELLOW, BLUE, MAGENTA, DIM, NC,
    say, header, emit_provider, run, mode, models, installed,
    cooldown_left, mark_rate_limited, log_usage, profile_conf,
)
from .limits import hit_limit, remember_limit, tail_hit_limit


def _without_prompt(argv, at):
    """Claude Code reads the prompt from stdin when -p has none."""
    return [*argv[:at], *argv[at + 1:]], argv[at]


def _prompt_from_stdin(argv, at):
    """Codex reads the prompt from stdin when it is `-`."""
    return [*argv[:at], '-', *argv[at + 1:]], argv[at]


def plain_prompt(prompt):
    """A prompt starting with a dash would be read as an option by the CLI, even in plan mode.
    A leading space keeps it text in every CLI, whether or not it supports `--`."""
    return ' ' + prompt if prompt.startswith('-') else prompt


def no_tools():
    """Set by the server for emulated function calling: the model may only write text."""
    return os.environ.get('SMART_AI_NO_TOOLS') == '1'


def _write_private(path, data):
    """Atomic, and readable only by the user from the first byte."""
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(data)
    os.replace(temporary, path)


def _credentials_backup(main_creds):
    return main_creds.with_name('.credentials.omni-backup')


def restore_credentials(main_creds):
    """Puts the main login back after a swap. Also heals a swap left behind by a process that was killed.
    An empty backup means there was no main login."""
    backup = _credentials_backup(main_creds)
    try:
        saved = backup.read_bytes()
    except OSError:
        return
    if saved:
        _write_private(main_creds, saved)
    else:
        main_creds.unlink(missing_ok=True)
    backup.unlink(missing_ok=True)


def _swap_credentials(main_creds, creds):
    # The backup reaches the disk before the main file changes, so no moment exists without a way back
    _write_private(_credentials_backup(main_creds), main_creds.read_bytes() if main_creds.exists() else b'')
    _write_private(main_creds, creds.read_bytes())


def _stop_on_sigterm(signum, frame):
    raise SystemExit(128 + signum)  # cancel and timeout send SIGTERM (Ctrl+Break on Windows); this makes `finally` run


# What cancel sends: SIGTERM, or Ctrl+Break on Windows
STOP_SIGNALS = tuple(getattr(signal, name) for name in ('SIGTERM', 'SIGBREAK') if hasattr(signal, name))


def run_claude(profile, prompt):
    if not installed('claude'):
        say(f'{RED}✗ Claude Code não instalado.{NC}')
        say(f'{DIM}  Execute o instalador: {REPO / "scripts" / ("install.ps1" if os.name == "nt" else "install.sh")}{NC}')
        return 1
    env = dict(os.environ)
    env.pop('CLAUDE_CONFIG_DIR', None)
    main_creds = Path('~/.claude/.credentials.json').expanduser()
    restore_credentials(main_creds)
    swapped = False
    previous_handler = None
    if profile == 'api-key' or profile.startswith('api-key:'):
        wanted = profile.partition(':')[2] or 'principal'
        env['ANTHROPIC_API_KEY'] = next((v for n, v, _ in config_tool.stored_keys('claude') if n == wanted),
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
                if threading.current_thread() is threading.main_thread():
                    previous_handler = {stop: signal.signal(stop, _stop_on_sigterm) for stop in STOP_SIGNALS}
                _swap_credentials(main_creds, creds)
                swapped = True
                say(f'{DIM}  → Credenciais trocadas para perfil: {profile}{NC}')
    emit_provider('claude', profile)
    header(f'{BLUE}🤖 Claude Code{NC}', f'perfil: {profile}')
    argv = [BINS['claude'], '--model', models()['claude']]
    argv += {'plan': ['--permission-mode', 'plan'], 'edits': ['--permission-mode', 'acceptEdits'],
             'auto': ['--permission-mode', 'auto'], 'yolo': ['--permission-mode', 'bypassPermissions']}.get(mode(), [])
    if no_tools():
        argv += ['--tools', '', '--strict-mcp-config']
    argv += ['-p', plain_prompt(prompt), '--verbose']
    at = len(argv) - 2
    if os.environ.get('SMART_AI_EVENTS') == '1':
        argv += ['--output-format', 'stream-json']
    try:
        code, output = run(argv, env, prompt_at=at, long_prompt=_without_prompt)
    finally:
        if swapped:
            restore_credentials(main_creds)
            for stop, handler in (previous_handler or {}).items():
                signal.signal(stop, handler)
    if hit_limit(output, code):
        remember_limit(f'claude:{profile}', output)
        mark_rate_limited(profile)
        log_usage('claude', profile, 'rate_limited')
        return RATE_LIMITED
    if code == 0:
        remember_limit(f'claude:{profile}', '')
    log_usage('claude', profile, 'success' if code == 0 else f'error:{code}')
    return code


def key_account(tool, name):
    """Limit/cooldown id of an API key: '<tool>:api-key' for the main saved one, '<tool>:api-key:<name>' for the others."""
    base = f'{tool}:api-key' if tool else 'api-key'
    return base if name == 'principal' else f'{base}:{name}'


def run_with_key_retry(tool, argv, env_var, label, long_prompt=None):
    """Main login first, then each extra account of the tool (/account add TOOL NAME), then the saved API key.
    The prompt is the last item of `argv`."""
    prompt = {'prompt_at': len(argv) - 1, 'long_prompt': long_prompt}
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
            emit_provider(tool)  # a new attempt: the reader forgets what the previous one failed with
        code, output = run(argv, dict(os.environ, **env) if env else None, **prompt)
        remember_limit(account, output if code != 0 else '')
        if code == 0:
            break
        if tail_hit_limit(output):
            mark_rate_limited(account)
    for name, key, origin in config_tool.stored_keys(tool):
        account = key_account(tool, name)
        if code == 0 or origin == 'env' or cooldown_left(account):
            continue
        say(f'{YELLOW}🔑 {label}: tentando com a chave {name}{NC}')
        emit_provider(tool)
        code, output = run(argv, dict(os.environ, **{env_var: key}), **prompt)
        remember_limit(account, output if code != 0 else '')
        if code != 0 and tail_hit_limit(output):
            mark_rate_limited(account)
    log_usage(tool, 'N/A', 'success' if code == 0 else 'error')
    return code


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


def run_codex(prompt):
    if no_tools():
        return 1  # its own tools cannot be switched off
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
    return run_with_key_retry('codex', argv + ['--', prompt], 'CODEX_API_KEY', 'Codex', _prompt_from_stdin)


def run_agy(prompt):
    if no_tools():
        return 1
    emit_provider('agy')
    header(f'{MAGENTA}🚀 Antigravity{NC}', 'economizando Claude')
    argv = [BINS['agy']]
    if os.environ.get('SMART_AI_EVENTS') == '1':
        argv += ['--output-format', 'stream-json']
    if models()['agy']:
        argv += ['--model', models()['agy']]
    argv += {'plan': ['--mode', 'plan'], 'edits': ['--mode', 'accept-edits'], 'auto': ['--mode', 'auto'],
             'yolo': ['--dangerously-skip-permissions']}.get(mode(), [])
    return run_with_key_retry('agy', argv + ['-p', plain_prompt(prompt)], 'GEMINI_API_KEY', 'Antigravity')


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
            emit_provider('openrouter')
        env = module_env(dict(os.environ, OPENROUTER_API_KEY=key, SMART_AI_OPENROUTER_PAID=values['openrouter_paid']))
        code, _ = run(argv, env, stdin=prompt)
        if code == 0:
            break
    log_usage('openrouter', 'N/A', 'success' if code == 0 else 'error')
    return code


def run_cursor(prompt):
    if no_tools():
        return 1
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
    # 'ask', not 'plan': in plan mode the headless agent was seen writing files anyway
    argv += {'plan': ['--mode', 'ask'], 'edits': ['--force'], 'auto': ['--force'], 'yolo': ['--force']}.get(mode(), [])
    # Subscription login first, then each saved CURSOR_API_KEY
    return run_with_key_retry('cursor', argv + [plain_prompt(prompt)], 'CURSOR_API_KEY', 'Cursor')


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
            emit_provider('deepseek')
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
        if no_tools():
            argv += ['--tools', '', '--disable-web-search']
        return run_with_key_retry('grok', argv + ['-p', plain_prompt(prompt)], 'XAI_API_KEY', 'Grok')
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
            emit_provider('grok')
        code, _ = run(argv, module_env(dict(os.environ, XAI_API_KEY=key)), stdin=prompt)
        if code == 0:
            break
    log_usage('grok', 'N/A', 'success' if code == 0 else 'error')
    return code
