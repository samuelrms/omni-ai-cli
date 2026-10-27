#!/usr/bin/env python3
"""Model and Claude account settings for Omni AI.

  python -m omni_ai.config env                     shell assignments (SMART_AI_* model variables)
  python -m omni_ai.config model [KEY [VALUE|default]]
  python -m omni_ai.config account list|add NAME|login NAME|remove NAME|enable NAME|disable NAME
  python -m omni_ai.config account list|add|login|remove|enable|disable codex|agy|grok NAME
  python -m omni_ai.config key [list|set PROVIDER|remove PROVIDER|get PROVIDER]
"""
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from .osutils import link, resolve_bin
from .tools import TOOLS, keys as tool_keys

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path(os.environ.get('SMART_AI_HOME', '~/.config/smart-ai')).expanduser()
MODELS_FILE = CONFIG_DIR / 'models.json'
ACCOUNTS_DIR = Path(os.environ.get('SMART_AI_ACCOUNTS', '~/.claude-accounts')).expanduser()
TEMPLATE_PROFILES = ROOT / 'profiles.json'


def profiles_path():
    """User's accounts and routing live outside the install folder, so updates never overwrite them."""
    if os.environ.get('SMART_AI_CONFIG'):
        return Path(os.environ['SMART_AI_CONFIG'])
    user = CONFIG_DIR / 'profiles.json'
    if not user.exists() and TEMPLATE_PROFILES.exists():
        user.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TEMPLATE_PROFILES, user)
    return user

# key: (default, env override, description); one per tool, plus the two settings that are not a model
MODELS = {name: (tool.model_default, tool.model_env, tool.model_help) for name, tool in TOOLS.items()}
MODELS = dict(list(MODELS.items())[:2] + [('codex_effort', ('high', 'SMART_AI_CODEX_EFFORT', 'Codex: minimal, low, medium, high, xhigh'))]
              + list(MODELS.items())[2:]
              + [('openrouter_paid', ('off', 'SMART_AI_OPENROUTER_PAID', 'OpenRouter: on autoriza modelos pagos (cobrados na sua conta)'))])
# provider: (env var the CLI reads, expected prefix or None, where to create one)
KEYS = tool_keys()
KEYS_DIR = CONFIG_DIR / 'keys'
LEGACY_OPENROUTER = CONFIG_DIR / 'openrouter_key'
EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh')
NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.replace(name, path)


def model_values():
    saved = read_json(MODELS_FILE, {})
    values = {}
    for key, (default, env, _) in MODELS.items():
        values[key] = os.environ.get(env) or saved.get(key) or default
    return values


def validate_model(key, value):
    if key not in MODELS:
        raise SystemExit(f'Chave inválida: {key}. Use: {", ".join(MODELS)}')
    if not re.fullmatch(r'[A-Za-z0-9._:/@\[\]-]*', value):
        raise SystemExit('Valor com caracteres inválidos.')
    if key == 'codex_effort' and value not in EFFORTS:
        raise SystemExit('Esforço inválido: ' + ', '.join(EFFORTS))
    if key == 'openrouter_paid' and value not in ('on', 'off'):
        raise SystemExit('Use: /model openrouter_paid on|off')
    if key == 'openrouter' and value and value != 'openrouter/free' and not value.endswith(':free') \
            and model_values()['openrouter_paid'] != 'on':
        raise SystemExit('Modelo pago não autorizado. Autorize antes com: /model openrouter_paid on')


def cmd_model(args):
    if not args:
        values = model_values()
        saved = read_json(MODELS_FILE, {})
        for key, (default, env, help_text) in MODELS.items():
            origin = 'env' if os.environ.get(env) else ('salvo' if saved.get(key) else 'padrão')
            print(f'  {key:<13} {values[key] or "(automático)":<24} [{origin}]  {help_text}')
        print('\nAlterar: /model codex gpt-6-astra · voltar ao padrão: /model codex default')
        return
    key = args[0]
    if len(args) == 1:
        validate_model(key, '' if key != 'codex_effort' else 'high')
        print(f"{key} = {model_values()[key] or '(automático)'}  ·  {MODELS[key][2]}")
        return
    value = args[1]
    saved = read_json(MODELS_FILE, {})
    if value == 'default':
        validate_model(key, MODELS[key][0] if key in MODELS else '')
        saved.pop(key, None)
    else:
        validate_model(key, value)
        saved[key] = value
    current = saved.get('openrouter', '')
    if key == 'openrouter_paid' and value == 'off' and current and current != 'openrouter/free' \
            and not current.endswith(':free'):
        saved.pop('openrouter')
        print('  Modelo pago do OpenRouter removido; voltou ao automático gratuito.')
    write_json(MODELS_FILE, saved)
    print(f'✓ {key} = {model_values()[key] or "(automático)"}')
    if key == 'openrouter_paid' and model_values()[key] == 'on':
        print('  Atenção: modelos pagos do OpenRouter serão cobrados na sua conta OpenRouter.')
        print('  Escolha o modelo com /model openrouter ID · desligue com /model openrouter_paid off')
    if os.environ.get(MODELS[key][1]):
        print(f'  Aviso: a variável {MODELS[key][1]} está definida e tem prioridade.')


def cmd_env():
    values = model_values()
    for key, (_, env, _) in MODELS.items():
        print(f'{env}={shlex.quote(values[key])}')


def profiles():
    data = read_json(profiles_path(), None)
    if data is None:
        raise SystemExit(f'Não consegui ler {profiles_path()}')
    data.setdefault('profiles', {})
    return data


def ordered(data):
    return sorted(data['profiles'].items(), key=lambda item: item[1].get('priority', 99))


def logged_in(profile):
    config_dir = profile.get('config_dir')
    env = dict(os.environ)
    if config_dir:
        folder = Path(config_dir).expanduser()
        if not folder.is_dir():
            return False
        env['CLAUDE_CONFIG_DIR'] = str(folder)
    elif profile.get('credentials_file') and not Path(profile['credentials_file']).expanduser().exists():
        return False
    try:
        result = subprocess.run([resolve_bin(os.environ.get('CLAUDE_BIN', 'claude')) or 'claude', 'auth', 'status', '--json'],
                                env=env, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        status = json.loads(result.stdout)
    except ValueError:
        return result.returncode == 0 and 'not logged' not in (result.stdout + result.stderr).lower()
    # "authMethod": "none" means no login, even though the command itself succeeds
    return bool(status.get('loggedIn', status.get('authMethod') not in (None, '', 'none')))


SHARED = ('skills', 'plugins', 'agents', 'commands', 'hooks', 'CLAUDE.md', 'settings.json')


def share_claude_setup(config_dir):
    """Give an isolated account the main account's skills, plugins and MCP servers; login stays separate."""
    main = Path(os.environ.get('SMART_AI_CLAUDE_HOME', '~/.claude')).expanduser()
    for item in SHARED:
        source, target = main / item, config_dir / item
        if source.exists() and not target.exists() and not target.is_symlink():
            link(target, source)
    main_state = read_json(main.parent / '.claude.json', {})
    servers = main_state.get('mcpServers')
    if servers:
        state_file = config_dir / '.claude.json'
        state = read_json(state_file, {})
        state['mcpServers'] = servers
        write_json(state_file, state)
        os.chmod(state_file, 0o600)


def login(name, profile):
    config_dir = Path(profile['config_dir']).expanduser()
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    print(f'Abrindo login do Claude para a conta "{name}" (pasta isolada {config_dir}).')
    print('Sua conta principal não é afetada.')
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config_dir))
    code = subprocess.call([resolve_bin(os.environ.get('CLAUDE_BIN', 'claude')) or 'claude', 'auth', 'login'], env=env)
    if code == 0:
        share_claude_setup(config_dir)
        print('Skills, plugins, CLAUDE.md, settings e MCPs compartilhados com a conta principal.')
    return code


def cmd_account(args):
    action = args[0] if args else 'list'
    if len(args) > 1 and args[1] in TOOL_HOMES:
        return cmd_tool_account(args[1], action, args[2:])
    if len(args) > 1 and args[1] == 'claude':
        args = args[:1] + args[2:]
    data = profiles()
    if action == 'sync-all':
        for name, profile in ordered(data):
            if profile.get('config_dir') and Path(profile['config_dir']).expanduser().is_dir():
                share_claude_setup(Path(profile['config_dir']).expanduser())
                print(f'  ✓ conta Claude "{name}" atualizada')
        return
    if action == 'list':
        for name, profile in ordered(data):
            state = {True: 'logada', False: 'sem login', None: '?'}[logged_in(profile)]
            enabled = 'ativa' if profile.get('enabled', True) else 'desativada'
            print(f"  {profile.get('priority', '?')}. {name:<12} {state:<10} {enabled:<11} {profile.get('name', '')}")
        for tool in TOOL_HOMES:
            extra = tool_accounts(tool)
            if extra:
                print(f'\n  {tool}: principal + ' + ', '.join(name for name, _ in extra))
        print('\nAdicionar: /account add NOME (Claude) · /account add codex|agy|grok NOME · refazer login: /account login NOME')
        return
    if len(args) < 2 or not NAME.fullmatch(args[1]):
        raise SystemExit('Informe um nome: letras minúsculas, números, - ou _ (ex.: /account add pessoal)')
    name = args[1]
    profile = data['profiles'].get(name)
    if action == 'add':
        if profile and profile.get('config_dir') and logged_in(profile):
            raise SystemExit(f'Conta "{name}" já existe e está logada. Use /account login {name} para trocar.')
        priority = max([p.get('priority', 0) for p in data['profiles'].values()] or [0]) + 1
        profile = dict(profile or {}, name=(profile or {}).get('name') or name, enabled=True,
                       status='active', last_rate_limit=None, cooldown_minutes=(profile or {}).get('cooldown_minutes', 60),
                       priority=(profile or {}).get('priority', priority), config_dir=str(ACCOUNTS_DIR / name))
        profile.pop('credentials_file', None)
        if login(name, profile) != 0:
            raise SystemExit('Login não concluído; nada foi alterado.')
        data['profiles'][name] = profile
        write_json(profiles_path(), data)
        print(f'✓ Conta "{name}" adicionada com prioridade {profile["priority"]}.')
    elif not profile:
        raise SystemExit(f'Conta "{name}" não existe. Veja /account list.')
    elif action == 'login':
        if not profile.get('config_dir'):
            if name == 'primary':
                print('A conta primary usa o login normal do Claude: rode "claude auth login".')
                return
            profile['config_dir'] = str(ACCOUNTS_DIR / name)
            profile.pop('credentials_file', None)
        if login(name, profile) == 0:
            write_json(profiles_path(), data)
            print(f'✓ Login da conta "{name}" atualizado.')
    elif action == 'sync':
        if not profile.get('config_dir'):
            print(f'A conta "{name}" usa a configuração principal; nada a sincronizar.')
            return
        share_claude_setup(Path(profile['config_dir']).expanduser())
        print(f'✓ Conta "{name}" sincronizada com skills, plugins e MCPs da principal.')
    elif action in ('enable', 'disable'):
        profile['enabled'] = action == 'enable'
        write_json(profiles_path(), data)
        print(f'✓ Conta "{name}" ' + ('ativada.' if profile['enabled'] else 'desativada.'))
    elif action == 'remove':
        if name == 'primary':
            raise SystemExit('A conta primary não pode ser removida; use /account disable primary.')
        del data['profiles'][name]
        write_json(profiles_path(), data)
        folder = profile.get('config_dir')
        if folder and Path(folder).expanduser().is_dir():
            # Log out first: on macOS the login lives in the Keychain, not only in the folder
            subprocess.run([resolve_bin(os.environ.get('CLAUDE_BIN', 'claude')) or 'claude', 'auth', 'logout'],
                           env=dict(os.environ, CLAUDE_CONFIG_DIR=str(Path(folder).expanduser())),
                           capture_output=True, timeout=30)
            shutil.rmtree(Path(folder).expanduser())
        print(f'✓ Conta "{name}" removida.')
    else:
        raise SystemExit('Ação inválida: list, add, login, sync, enable, disable, remove')


# Extra subscription accounts for the other CLIs. Each one gets its own home folder (the env var the CLI
# reads); the main login keeps the normal folder. Shared items are linked so every account sees the same setup.
TOOL_HOMES = {
    'codex': ('CODEX_HOME', '~/.codex', ('config.toml', 'AGENTS.md', 'prompts', 'skills'), ['codex', 'login']),
    'grok': ('GROK_HOME', '~/.grok', ('config.toml', 'AGENTS.md', 'skills', 'plugins', 'hooks'), ['grok', 'login']),
    # agy has no folder variable, so the account runs under its own HOME; git, ssh and gh keep working through links
    'agy': ('HOME', '~', ('.gitconfig', '.ssh', '.config/gh', '.gemini/config'), ['agy']),
}
TOOL_ACCOUNTS_DIR = CONFIG_DIR / 'accounts'


def tool_accounts(tool, enabled_only=False):
    """Extra accounts of a tool by priority; the main login is not listed (it always runs first)."""
    items = sorted(profiles().get('accounts', {}).get(tool, {}).items(), key=lambda item: item[1].get('priority', 99))
    return [(name, conf) for name, conf in items if conf.get('enabled', True) or not enabled_only]


def tool_env(tool, conf):
    return {TOOL_HOMES[tool][0]: str(Path(conf['config_dir']).expanduser())}


def tool_logged_in(tool, conf):
    folder = Path(conf['config_dir']).expanduser()
    if not folder.is_dir():
        return False
    if tool == 'codex':
        try:
            return subprocess.run([resolve_bin('codex') or 'codex', 'login', 'status'], env=dict(os.environ, **tool_env(tool, conf)),
                                  capture_output=True, timeout=20).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return None
    return (folder / ('auth.json' if tool == 'grok' else '.gemini/jetski-standalone-oauth-token')).is_file()


def share_tool_setup(tool, folder):
    main = Path(TOOL_HOMES[tool][1]).expanduser()
    for item in TOOL_HOMES[tool][2]:
        source, target = main / item, folder / item
        if source.exists() and not target.exists() and not target.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            link(target, source)


def tool_login(tool, name, conf):
    folder = Path(conf['config_dir']).expanduser()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    share_tool_setup(tool, folder)
    print(f'Abrindo login do {tool} para a conta "{name}" (pasta isolada {folder}).')
    print('Sua conta principal não é afetada.' + (' Faça o login com o Google e saia com Ctrl-C.' if tool == 'agy' else ''))
    command = TOOL_HOMES[tool][3]
    return subprocess.call([resolve_bin(command[0]) or command[0]] + command[1:], env=dict(os.environ, **tool_env(tool, conf)))


def cmd_tool_account(tool, action, args):
    data = profiles()
    accounts = data.setdefault('accounts', {}).setdefault(tool, {})
    if action == 'list':
        print(f'  {tool}: principal (login normal) sempre primeiro')
        for name, conf in tool_accounts(tool):
            state = {True: 'logada', False: 'sem login', None: '?'}[tool_logged_in(tool, conf)]
            print(f"  {conf.get('priority', '?')}. {name:<12} {state:<10} {'ativa' if conf.get('enabled', True) else 'desativada'}")
        print(f'\nAdicionar: /account add {tool} NOME · refazer login: /account login {tool} NOME')
        return
    if not args or not NAME.fullmatch(args[0]) or args[0] == 'primary':
        raise SystemExit(f'Informe um nome: letras minúsculas, números, - ou _ (ex.: /account add {tool} trabalho)')
    name = args[0]
    conf = accounts.get(name)
    if action == 'add':
        if conf and tool_logged_in(tool, conf):
            raise SystemExit(f'Conta "{name}" do {tool} já existe e está logada. Use /account login {tool} {name} para trocar.')
        conf = conf or {'priority': max([c.get('priority', 0) for c in accounts.values()] or [0]) + 1,
                        'config_dir': str(TOOL_ACCOUNTS_DIR / tool / name), 'enabled': True}
        tool_login(tool, name, conf)
        if tool_logged_in(tool, conf) is False:
            raise SystemExit('Login não concluído; nada foi alterado.')
        accounts[name] = conf
        write_json(profiles_path(), data)
        print(f'✓ Conta "{name}" do {tool} adicionada com prioridade {conf["priority"]}.')
    elif not conf:
        raise SystemExit(f'Conta "{name}" do {tool} não existe. Veja /account list {tool}.')
    elif action == 'login':
        tool_login(tool, name, conf)
    elif action in ('enable', 'disable'):
        conf['enabled'] = action == 'enable'
        write_json(profiles_path(), data)
        print(f'✓ Conta "{name}" do {tool} ' + ('ativada.' if conf['enabled'] else 'desativada.'))
    elif action == 'remove':
        del accounts[name]
        write_json(profiles_path(), data)
        folder = Path(conf['config_dir']).expanduser()
        if tool in ('codex', 'grok') and folder.is_dir():
            subprocess.run([resolve_bin(tool) or tool, 'logout'], env=dict(os.environ, **tool_env(tool, conf)),
                           capture_output=True, timeout=30)
        if folder.is_dir() and folder.is_relative_to(TOOL_ACCOUNTS_DIR):
            shutil.rmtree(folder)
        print(f'✓ Conta "{name}" do {tool} removida.')
    else:
        raise SystemExit('Ação inválida: list, add, login, enable, disable, remove')


def key_path(provider):
    return KEYS_DIR / provider


def extra_key_path(provider, name):
    return KEYS_DIR / f'{provider}.{name}'


def stored_keys(provider):
    """Every key of a tool in the order they are tried: the env variable, the main saved key, then the
    extra ones (/key add TOOL NAME) by name. Each item: (name, value, origin)."""
    found = []
    env = os.environ.get(KEYS[provider][0])
    if env:
        found.append(('env', env, 'env'))
    for path in (key_path(provider), LEGACY_OPENROUTER if provider == 'openrouter' else None):
        if path and path.is_file() and path.read_text().strip():
            found.append(('principal', path.read_text().strip(), 'salva'))
            break
    extras = sorted(KEYS_DIR.glob(provider + '.*'), key=lambda p: (len(p.name), p.name)) if KEYS_DIR.is_dir() else []
    for path in extras:
        value = path.read_text().strip()
        if value:
            found.append((path.name[len(provider) + 1:], value, 'salva'))
    return found


def stored_key(provider):
    keys = stored_keys(provider)
    if keys:
        return keys[0][1], keys[0][2]
    return '', ''


def ask_key(provider, inline):
    env, prefix, url = KEYS[provider]
    if inline:
        value = inline.strip()  # typed inline: /key openrouter sk-or-...
    else:
        print(f'Chave de {provider} ({url}). Cole e tecle Enter; ela não aparece na tela.')
        try:
            value = getpass.getpass('> ').strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit('\nCancelado.')
    if not value or not re.fullmatch(r'[A-Za-z0-9._-]{16,}', value):
        raise SystemExit('Chave vazia ou com formato inválido.')
    if prefix and not value.startswith(prefix):
        raise SystemExit(f'Chave de {provider} deve começar com {prefix}')
    return value


def save_key(path, value):
    KEYS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(KEYS_DIR, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)
    os.chmod(path, 0o600)


def next_key_name(provider):
    taken = {name for name, _, _ in stored_keys(provider)}
    return next(f'chave{n}' for n in range(2, 1000) if f'chave{n}' not in taken)


def cmd_key(args):
    """/key list · /key TOOL [value] (main key) · /key add TOOL [NAME] [value] (one more key)
    · /key remove TOOL [NAME]. Keys of a tool are tried in order when one fails or hits its limit."""
    action = args[0] if args else 'list'
    if action == 'list':
        for provider, (env, _, url) in KEYS.items():
            keys = stored_keys(provider)
            if not keys:
                print(f'  {provider:<11} {"—":<34} {env:<19} {url}')
            for i, (name, value, origin) in enumerate(keys):
                shown = f'{name}: {value[:7]}…{value[-4:]} [{origin}]'
                print(f'  {provider if i == 0 else "":<11} {shown:<34} {env if i == 0 else "":<19} {url if i == 0 else ""}')
        print('\nPrincipal: /key claude · mais uma: /key add claude [NOME] · remover: /key remove claude [NOME]')
        print('Assinatura vem primeiro; as chaves são usadas em ordem quando ela falha ou bate no limite.')
        print('OpenRouter funciona só com chave.')
        return
    if action in KEYS:
        action = 'set'
    else:
        args = args[1:]
    if not args or args[0] not in KEYS:
        raise SystemExit('Informe a ferramenta: ' + ', '.join(KEYS))
    provider = args[0]
    env = KEYS[provider][0]
    if action == 'get':
        print(stored_key(provider)[0])
    elif action == 'set':
        save_key(key_path(provider), ask_key(provider, args[1] if len(args) > 1 else ''))
        print(f'✓ Chave principal de {provider} salva em {key_path(provider)} (permissão 600).')
        if os.environ.get(env):
            print(f'  Aviso: a variável {env} está definida e é tentada primeiro.')
    elif action == 'add':
        rest = args[1:]
        name = rest.pop(0) if rest and NAME.fullmatch(rest[0]) and len(rest[0]) < 16 else next_key_name(provider)
        if name in ('env', 'principal') or extra_key_path(provider, name).exists():
            raise SystemExit(f'Já existe uma chave {name} para {provider}.')
        if not key_path(provider).is_file() and not (provider == 'openrouter' and LEGACY_OPENROUTER.is_file()):
            path, name = key_path(provider), 'principal'
        else:
            path = extra_key_path(provider, name)
        save_key(path, ask_key(provider, rest[0] if rest else ''))
        print(f'✓ Chave {name} de {provider} salva ({len(stored_keys(provider))} no total, usadas em ordem).')
    elif action == 'remove':
        name = args[1] if len(args) > 1 else 'principal'
        paths = [key_path(provider), LEGACY_OPENROUTER if provider == 'openrouter' else None] if name == 'principal' \
            else [extra_key_path(provider, name) if NAME.fullmatch(name) else None]
        removed = False
        for path in paths:
            if path and path.exists():
                path.unlink()
                removed = True
        print(f'✓ Chave {name} de {provider} removida.' if removed else f'Nenhuma chave {name} salva para {provider}.')
    else:
        raise SystemExit('Ação inválida: list, set, add, remove')


def enabled_profiles():
    """Names of enabled profiles by priority, for the fallback loop."""
    return [name for name, profile in ordered(profiles()) if profile.get('enabled', True)]


def cmd_profiles():
    for name in enabled_profiles():
        print(name)


def main(argv):
    command, args = (argv[0], argv[1:]) if argv else ('model', [])
    if command == 'env':
        cmd_env()
    elif command == 'model':
        cmd_model(args)
    elif command == 'account':
        cmd_account(args)
    elif command == 'key':
        cmd_key(args)
    elif command == 'profiles':
        cmd_profiles()
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main(sys.argv[1:])
