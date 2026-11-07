"""Status, version and health-check commands: what is installed, logged in and configured."""
import json
import os
import subprocess
from pathlib import Path
import urllib.error
import urllib.request

from .. import config as config_tool
from ..osutils import REPO, resolve_bin
from ..tools import CLIS, TOOLS
from .base import (
    BINS, LOG_FILE, RED, GREEN, YELLOW, CYAN, BOLD, DIM, NC,
    say, installed, mode, cooldown_left,
)

# A cheap authenticated call per provider. Cursor has no public endpoint to check a key against.
KEY_CHECKS = {
    'claude': ('https://api.anthropic.com/v1/models', lambda key: {'x-api-key': key, 'anthropic-version': '2023-06-01'}),
    'codex': ('https://api.openai.com/v1/models', None),
    'agy': ('https://generativelanguage.googleapis.com/v1beta/models', lambda key: {'x-goog-api-key': key}),
    'openrouter': ('https://openrouter.ai/api/v1/key', None),
    'grok': ('https://api.x.ai/v1/models', None),
    'deepseek': ('https://api.deepseek.com/models', None),
    'groq': ('https://api.groq.com/openai/v1/models', None),
    'cerebras': ('https://api.cerebras.ai/v1/models', None),
}
REJECTS_WITH_400 = ('agy', 'grok')  # these two answer 400, not 401, to an invalid key


def key_problem(provider, key):
    """Why a saved key cannot work, or None. A network failure says nothing about the key, so it is not a problem."""
    prefix = config_tool.KEYS[provider][1]
    if prefix and not key.startswith(prefix):
        return f'formato inválido (esperado {prefix}...)'
    check = KEY_CHECKS.get(provider)
    if not check:
        return None
    url, headers = check
    headers = headers(key) if headers else {'Authorization': f'Bearer {key}'}
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={**headers, 'User-Agent': 'omni-ai-cli'}), timeout=5).close()
    except urllib.error.HTTPError as error:
        return f'recusada pelo provedor (HTTP {error.code})' if (
            error.code in (401, 403) or (error.code == 400 and provider in REJECTS_WITH_400)) else None
    except (OSError, ValueError):
        return None
    return None


def version(tool):
    try:
        out = subprocess.run([resolve_bin(BINS[tool]), '--version'], capture_output=True, text=True, encoding='utf-8',
                             errors='replace', timeout=15).stdout
        return out.strip().splitlines()[0] if out.strip() else '?'
    except (OSError, subprocess.TimeoutExpired, TypeError):
        return '?'


def service_lines():
    """(healthy, text) about the `ai serve` service, or None when it is not installed."""
    from ..server import service
    info = service.describe()
    if not info:
        return None
    running, problem, _ = info
    return running and not problem, problem or ('rodando' if running else 'parado')


def show_status():
    say(f'{BOLD}{CYAN}  🧠 Omni AI CLI{NC}\n')
    line = service_lines()
    if line:
        say(f'  Serviço ai serve: {line[1]}\n')
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
    for tool, label in ((name, TOOLS[name].label) for name in CLIS):
        say(f'  {GREEN}●{NC} {label}: {version(tool)}' if installed(tool) else f'  {RED}○{NC} {label}: não instalado')
    if LOG_FILE.is_file():
        lines = LOG_FILE.read_text(encoding='utf-8', errors='replace').splitlines()
        counts = {t: sum(f'tool={t} ' in l for l in lines) for t in TOOLS}
        say(f'\n{BOLD}  📊 Estatísticas de uso:{NC}')
        say(f'    Total de chamadas: {len(lines)}')
        say('    ' + ' | '.join(f'{t}: {n}' for t, n in counts.items()))
    say()


def ai_version():
    from .whats_new import installed_version
    return installed_version()


def doctor():
    """Health check: what is installed, logged in and configured, and what is missing. Returns 1 when
    nothing can answer a request (no Claude login and no API key), 0 otherwise."""
    import platform
    ok, warn, bad = f'{GREEN}✓{NC}', f'{YELLOW}!{NC}', f'{RED}✗{NC}'
    problems = []

    say(f'{BOLD}{CYAN}  🩺 Omni AI CLI — diagnóstico{NC}\n')

    say(f'{BOLD}  Sistema{NC}')
    say(f'  {ok} Omni AI CLI {ai_version()}')
    say(f'  {ok} Python {platform.python_version()} · {platform.system()} {platform.machine()}')
    local_bin = str(Path('~/.local/bin').expanduser())
    if local_bin in os.environ.get('PATH', '').split(os.pathsep):
        say(f'  {ok} ~/.local/bin no PATH')
    else:
        say(f'  {warn} ~/.local/bin fora do PATH (adicione ao shell para o comando `ai`)')
        problems.append('~/.local/bin não está no PATH')
    say()

    say(f'{BOLD}  Configuração{NC}')
    cfg = config_tool.CONFIG_DIR
    say(f'  {ok if cfg.is_dir() else warn} {cfg}')
    say()

    say(f'{BOLD}  Contas Claude{NC}')
    logged = 0
    profiles = config_tool.ordered(config_tool.profiles())
    for name, conf in profiles:
        if not conf.get('enabled', True):
            say(f'  {warn} {name}: desabilitado')
            continue
        has_login = bool(conf.get('config_dir')) or (
            conf.get('credentials_file') and Path(conf['credentials_file']).expanduser().is_file())
        left = cooldown_left(name)
        if has_login and left:
            say(f'  {warn} {name}: em cooldown ({left // 60}m{left % 60}s)')
            logged += 1
        elif has_login:
            say(f'  {ok} {name}: logado')
            logged += 1
        else:
            say(f'  {bad} {name}: sem login (ai --account login {name})')
    if not logged:
        say(f'  {warn} nenhuma conta Claude logada')
    say()

    say(f'{BOLD}  Chaves de API{NC}')
    keys = [p for p in config_tool.KEYS if config_tool.stored_key(p)[0]]
    say(f"  {ok if keys else warn} {', '.join(keys) if keys else 'nenhuma configurada (ai --key)'}")
    for provider in config_tool.KEYS:
        for name, value, _ in config_tool.stored_keys(provider):
            problem = key_problem(provider, value)
            if problem:
                say(f'  {warn} {provider} ({name}): {problem}')
                problems.append(f'chave {name} de {provider}: {problem}. Troque com ai --key {provider}')
    say()

    say(f'{BOLD}  Ferramentas{NC}')
    for name in CLIS:
        label = TOOLS[name].label
        say(f'  {ok} {label}: {version(name)}' if installed(name) else f'  {warn} {label}: não instalado')
    say()

    line = service_lines()
    if line:
        say(f'{BOLD}  Serviço ai serve{NC}\n  {ok if line[0] else warn} {line[1]}\n')
        if not line[0]:
            problems.append(f'serviço ai serve: {line[1]}')

    classifiers = [p for p in ('groq', 'cerebras') if config_tool.stored_key(p)[0]]
    if classifiers:
        say(f'  {ok} Classificador: {", ".join(classifiers)} (rápido e grátis)')
    else:
        say(f'  {warn} Sem chave de classificador (Groq/Cerebras); usa OpenRouter grátis ou palavras-chave')
    say()

    if not logged and not keys:
        problems.append('nenhuma conta Claude logada e nenhuma chave de API: o roteador não tem como responder')

    if problems:
        say(f'{BOLD}{YELLOW}  Pendências:{NC}')
        for p in problems:
            say(f'  {warn} {p}')
        say(f'\n{DIM}  Rode `ai --setup` para um guia de primeiro acesso.{NC}\n')
    else:
        say(f'{GREEN}  Tudo pronto.{NC}\n')
    return 1 if not logged and not keys else 0
