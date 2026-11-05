"""Isolated Claude accounts and the extra per-tool accounts (codex/grok/agy): list, add, login, sync, remove."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..osutils import headless, link, resolve_bin
from .store import ACCOUNTS_DIR, CONFIG_DIR, NAME, read_json, write_json, profiles_path
from .profiles import profiles, ordered


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
        status = None
    if not isinstance(status, dict):
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
    try:
        code = subprocess.call([resolve_bin(os.environ.get('CLAUDE_BIN', 'claude')) or 'claude', 'auth', 'login'], env=env)
    except OSError as error:
        print(f'✗ Não foi possível iniciar o login do Claude: {error}', file=sys.stderr)
        return 1
    if code == 0:
        share_claude_setup(config_dir)
        print('Skills, plugins, CLAUDE.md, settings e MCPs compartilhados com a conta principal.')
    return code


def login_command(tool):
    """Codex's default login waits on a localhost callback; its device code works from anywhere."""
    command = list(MAIN_LOGINS[tool])
    if tool == 'codex' and headless():
        command.append('--device-auth')
    return command


def main_login(tool):
    """The tool's normal login, not an extra account: what a new machine or container needs first."""
    command = login_command(tool)
    print(f'Abrindo o login principal do {tool}. Sem navegador nesta máquina, abra o link em outro dispositivo.')
    try:
        return subprocess.call([resolve_bin(os.environ.get(tool.upper() + '_BIN', command[0])) or command[0]] + command[1:])
    except OSError as error:
        print(f'✗ Não foi possível iniciar o login do {tool}: {error}', file=sys.stderr)
        return 1


def cmd_account(args):
    action = args[0] if args else 'list'
    if action == 'login':
        if len(args) == 1:
            raise SystemExit('Informe o que logar: ai --account login primary|codex|grok|cursor (principal) '
                             'ou ai --account login [codex|grok|agy] NOME (conta extra)')
        if args[1] == 'primary' or (len(args) == 2 and args[1] in ('claude', 'codex', 'grok', 'cursor')):
            code = main_login('claude' if args[1] == 'primary' else args[1])
            if code:
                raise SystemExit(code)
            return
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
        resolved = Path(folder).expanduser() if folder else None
        if resolved and resolved.is_dir() and resolved.is_relative_to(ACCOUNTS_DIR):
            # Log out first: on macOS the login lives in the Keychain, not only in the folder
            try:
                subprocess.run([resolve_bin(os.environ.get('CLAUDE_BIN', 'claude')) or 'claude', 'auth', 'logout'],
                               env=dict(os.environ, CLAUDE_CONFIG_DIR=str(resolved)),
                               capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass  # remove the folder regardless, so the account isn't left half-removed
            shutil.rmtree(resolved, ignore_errors=True)
        elif resolved and resolved.is_dir():
            # A hand-edited config_dir outside the managed folder (e.g. ~/.claude) is never deleted
            print(f'  ⚠ pasta fora de {ACCOUNTS_DIR} não removida por segurança: {resolved}')
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
MAIN_LOGINS = {'claude': ['claude', 'auth', 'login'], 'codex': ['codex', 'login'], 'grok': ['grok', 'login'],
               'cursor': ['cursor-agent', 'login']}


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
    command = login_command(tool) if tool in MAIN_LOGINS else TOOL_HOMES[tool][3]
    try:
        return subprocess.call([resolve_bin(command[0]) or command[0]] + command[1:], env=dict(os.environ, **tool_env(tool, conf)))
    except OSError as error:
        print(f'✗ Não foi possível iniciar o login do {tool}: {error}', file=sys.stderr)
        return 1


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
            try:
                subprocess.run([resolve_bin(tool) or tool, 'logout'], env=dict(os.environ, **tool_env(tool, conf)),
                               capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass  # remove the folder regardless
        if folder.is_dir() and folder.is_relative_to(TOOL_ACCOUNTS_DIR):
            shutil.rmtree(folder, ignore_errors=True)
        print(f'✓ Conta "{name}" do {tool} removida.')
    else:
        raise SystemExit('Ação inválida: list, add, login, enable, disable, remove')
