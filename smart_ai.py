#!/usr/bin/env python3
"""Smart AI router: the `ai` command on macOS, Linux and Windows.

  ai                                  open the chat
  ai "question"                       automatic routing
  ai --claude|--codex|--agy|--openrouter "question"
  ai --mode plan|edits|auto|yolo "question"
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

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import config_tool  # noqa: E402
from platform_utils import enable_ansi, resolve_bin  # noqa: E402

STATE_FILE = Path(os.environ.get('SMART_AI_STATE', '~/.cache/smart-ai/state.json')).expanduser()
LOG_FILE = Path(os.environ.get('SMART_AI_LOG', '~/.cache/smart-ai/usage.log')).expanduser()
BINS = {tool: os.environ.get(tool.upper() + '_BIN', tool) for tool in ('claude', 'codex', 'agy')}
MODES = ('default', 'plan', 'edits', 'auto', 'yolo')
RATE_LIMIT = re.compile(r'rate.?limit|too many requests|429|usage limit|exceeded.*limit|out of.*messages', re.I)
RATE_LIMITED = 2

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
        say(f'{DIM}  Execute o instalador: {ROOT / ("install.ps1" if os.name == "nt" else "install.sh")}{NC}')
        return 1
    env = dict(os.environ)
    env.pop('CLAUDE_CONFIG_DIR', None)
    main_creds = Path('~/.claude/.credentials.json').expanduser()
    backup = None
    if profile == 'api-key':
        env['ANTHROPIC_API_KEY'] = config_tool.stored_key('claude')[0]
    else:
        conf = profile_conf(profile)
        config_dir = conf.get('config_dir')
        if config_dir:
            folder = Path(config_dir).expanduser()
            if not folder.is_dir():
                say(f"{RED}✗ Conta '{profile}' sem login.{NC}")
                say(f'{DIM}  Execute: ai --account login {profile}{NC}')
                return 1
            env['CLAUDE_CONFIG_DIR'] = str(folder)
        elif conf.get('credentials_file'):
            # Legacy profiles: swap the credentials file for this call only
            creds = Path(conf['credentials_file']).expanduser()
            if creds != main_creds:
                if not creds.is_file():
                    say(f'{RED}✗ Arquivo de credenciais não encontrado: {creds}{NC}')
                    say(f'{DIM}  Execute: ai --account add {profile}{NC}')
                    return 1
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
    if RATE_LIMIT.search(output) or (code == 1 and re.search(r'error|limit|quota', output, re.I)):
        mark_rate_limited(profile)
        log_usage('claude', profile, 'rate_limited')
        return RATE_LIMITED
    log_usage('claude', profile, 'success' if code == 0 else f'error:{code}')
    return code


def run_with_key_retry(tool, argv, env_var, label):
    """Subscription first; if it fails and an API key is saved, try once more with the key."""
    code, _ = run(argv)
    key = config_tool.stored_key(tool)[0]
    if code != 0 and not os.environ.get(env_var) and key:
        say(f'{YELLOW}🔑 {label}: tentando com chave de API{NC}')
        code, _ = run(argv, dict(os.environ, **{env_var: key}))
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
    key = config_tool.stored_key('openrouter')[0]
    if not key:
        say(f'{RED}✗ OPENROUTER_API_KEY não configurada.{NC}')
        say(f'{DIM}  Execute: ai --key openrouter (ou /key openrouter no chat){NC}')
        log_usage('openrouter', 'N/A', 'no_api_key')
        return 1
    values = models()
    paid = values['openrouter_paid'] == 'on'
    header(f'{YELLOW}🌐 OpenRouter{NC}', ('modelos pagos autorizados' if paid else 'modelo gratuito') + ' + PII masking')
    argv = [sys.executable, str(ROOT / 'openrouter_client.py')]
    if values['openrouter']:
        argv += ['--model', values['openrouter']]
    env = dict(os.environ, OPENROUTER_API_KEY=key, SMART_AI_OPENROUTER_PAID=values['openrouter_paid'])
    code, _ = run(argv, env, stdin=prompt)
    log_usage('openrouter', 'N/A', 'success' if code == 0 else 'error')
    return code


# ─── routing ─────────────────────────────────────────────────────

def classify(prompt):
    prompt = os.environ.get('SMART_AI_CLASSIFY_PROMPT') or prompt
    lower = prompt.lower()
    routing = config().get('routing', {})
    if any(k in lower for k in routing.get('complex_keywords', [])):
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
        result = run_claude(profile, prompt)
        if result == 0:
            return 0
        if result != RATE_LIMITED:
            return result
        say(f'\n{YELLOW}🔄 Tentando próximo perfil...{NC}\n')
    if config_tool.stored_key('claude')[0] and not cooldown_left('api-key'):
        say(f'{YELLOW}🔑 Claude: tentando com chave de API{NC}')
        result = run_claude('api-key', prompt)
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
    forced = {'claude': claude_with_fallback, 'codex': run_codex, 'agy': run_agy, 'openrouter': run_openrouter}
    if tool in forced:
        return forced[tool](prompt)
    if classify(prompt) == 'complex':
        say(f'{DIM}📊 Classificado como: {CYAN}query complexa{NC}{DIM} → usando Claude{NC}\n')
        return claude_with_fallback(prompt)
    say(f'{DIM}📊 Classificado como: {YELLOW}query simples{NC}{DIM} → roteando para alternativa{NC}\n')
    routing = config().get('routing', {})
    order = [routing.get('prefer_tool_for_simple', 'codex')] + routing.get('fallback_order', ['openrouter', 'codex', 'agy'])
    for name in dict.fromkeys(t for t in order if t in ('openrouter', 'codex', 'agy')):
        if name != 'openrouter' and not installed(name):
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
    say(f'{BOLD}{CYAN}  🧠 Smart AI CLI{NC}\n')
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
    for tool, label in (('claude', 'Claude Code'), ('codex', 'Codex CLI'), ('agy', 'Antigravity')):
        say(f'  {GREEN}●{NC} {label}: {version(tool)}' if installed(tool) else f'  {RED}○{NC} {label}: não instalado')
    if LOG_FILE.is_file():
        lines = LOG_FILE.read_text(errors='replace').splitlines()
        counts = {t: sum(f'tool={t} ' in l for l in lines) for t in ('claude', 'codex', 'agy', 'openrouter')}
        say(f'\n{BOLD}  📊 Estatísticas de uso:{NC}')
        say(f'    Total de chamadas: {len(lines)}')
        say('    ' + ' | '.join(f'{t}: {n}' for t, n in counts.items()))
    say()


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
    if (ROOT / '.git').is_dir() and resolve_bin('git'):
        code = subprocess.call(['git', '-C', str(ROOT), 'pull', '--ff-only'])
        say(f'{GREEN}✓ Smart AI atualizado.{NC}' if code == 0 else f'{RED}✗ Não consegui atualizar; veja a mensagem acima.{NC}')
        return code
    if os.environ.get('SMART_AI_INSTALLER') == 'npm':
        command = 'npm install -g omni-ai-cli@latest'
        say(f'Atualizando com: {command}')
        npm = resolve_bin('npm')
        return subprocess.call([npm, 'install', '-g', 'omni-ai-cli@latest']) if npm else 1
    if os.name == 'nt':
        command = 'irm https://raw.githubusercontent.com/samuelrms/smart-ai-cli/main/get.ps1 | iex'
    else:
        command = 'curl -fsSL https://raw.githubusercontent.com/samuelrms/smart-ai-cli/main/get.sh | bash'
    say(f'Instalação sem git. Para atualizar, rode:\n  {command}')
    return 1


USAGE = f'''{BOLD}Uso:{NC}
  ai                                 Abrir o chat (/help dentro dele)
  ai "pergunta"                      Roteamento automático
  ai --claude|--codex|--agy|--openrouter "pergunta"
  ai --mode plan|edits|auto|yolo "pergunta"
  ai --model [chave valor]           Modelos
  ai --account [list|add|login|sync|remove] NOME
  ai --key [claude|codex|agy|openrouter|remove X]
  ai --sync [--dry-run]              MCPs e skills entre Claude, Codex e agy
  ai --status | --reset | --log | --classify "prompt"
  ai --update                        Atualizar para a versão mais nova
'''


def main(argv):
    enable_ansi()
    if not argv and sys.stdin.isatty():
        import terminal
        return terminal.main([])
    head = argv[0] if argv else ''
    delegated = {'--model': ('config_tool', ['model']), '-m': ('config_tool', ['model']),
                 '--account': ('config_tool', ['account']), '--key': ('config_tool', ['key']),
                 '--keys': ('config_tool', ['key']), '--setup-openrouter': ('config_tool', ['key', 'set', 'openrouter']),
                 '--setup-secondary': ('config_tool', ['account', 'add', 'secondary']),
                 '--setup': ('config_tool', ['account', 'add', 'secondary'])}
    if head in ('--chat', '--interactive', '-i', '--resume', '--sessions'):
        import terminal
        return terminal.main(argv[1:] if head in ('--chat', '--interactive', '-i') else argv)
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
        import sync_tool
        return sync_tool.main(argv[1:])
    tool, words, i = 'auto', [], 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--':
            words += argv[i + 1:]
            break
        if arg in ('--claude', '-c', '--codex', '-x', '--agy', '-a', '--openrouter', '-o'):
            tool = {'c': 'claude', 'x': 'codex', 'a': 'agy', 'o': 'openrouter'}.get(arg.lstrip('-'), arg.lstrip('-'))
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


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
