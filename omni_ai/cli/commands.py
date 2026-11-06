"""Management commands: history listing, shell completions, cooldown reset, log view, self-update."""
import json
import os
import subprocess

from .. import config as config_tool
from . import whats_new
from ..osutils import REPO, clean_terminal, resolve_bin
from ..tools import TOOLS
from .base import (
    LOG_FILE, STATE_FILE, RED, GREEN, YELLOW, CYAN, BOLD, DIM, NC,
    say,
)

SOURCE_LABEL = {'omni': f'{CYAN}omni{NC}', 'claude': f'{YELLOW}claude{NC}', 'codex': f'{GREEN}codex{NC}',
                'cursor': 'cursor', 'agy': 'antigravity'}


def history_command(args):
    """`ai history [list] [--source X] [--json]` lists past conversations across tools;
    `ai history <id> [--json]` prints one. Read-only."""
    from .. import history as history_mod
    as_json = '--json' in args
    rest = [a for a in args if a != '--json']
    source = None
    if '--source' in rest:
        idx = rest.index('--source')
        source = rest[idx + 1] if idx + 1 < len(rest) else None
        rest = rest[:idx] + rest[idx + 2:]
    target = next((a for a in rest if a != 'list'), None)

    if target:
        conv = history_mod.conversation(target)
        if not conv:
            if as_json:
                say('null')
            else:
                say(f'{RED}✗ Conversa não encontrada: {target}{NC}')
            return 1
        if as_json:
            say(json.dumps(conv, ensure_ascii=False))
            return 0
        conv['title'] = clean_terminal(conv['title'])
        say(f"{BOLD}{conv['title']}{NC} {DIM}· {SOURCE_LABEL.get(conv['source'], conv['source'])} · {conv.get('cwd', '')}{NC}\n")
        for m in conv['messages']:
            who = f'{CYAN}você{NC}' if m['role'] == 'user' else f'{GREEN}●{NC}'
            say(f"{who} {clean_terminal(m['content'])}\n")
        return 0

    items = history_mod.sessions([source] if source else None)
    if as_json:
        say(json.dumps(items, ensure_ascii=False))
        return 0
    if not items:
        say(f'{DIM}Nenhuma conversa encontrada.{NC}')
        return 0
    import time as _time
    for it in items:
        when = _time.strftime('%d/%m %H:%M', _time.localtime(it.get('updated') or 0))
        say(f"{SOURCE_LABEL.get(it['source'], it['source'])} {DIM}{when} · {it['count']}msg{NC}  {clean_terminal(it['title'])}")
        say(f"  {DIM}{it['id']}{NC}")
    return 0


def completion_words():
    """Every top-level word `ai` accepts, for shell completion."""
    flags = ['--model', '--account', '--key', '--keys', '--sync', '--usage', '--limits', '--setup',
             '--install', '--status', '--doctor', '--reset', '--log', '--classify', '--update',
             '--mode', '--chat', '--resume', '--sessions', '--new', '--help', '--version', '--completions', 'history', 'serve']
    return flags + ['--' + name for name in TOOLS] + ['doctor']


def completions(shell):
    """A completion script for `ai`, printed for the user to source. Completes the top-level flags."""
    words = ' '.join(completion_words())
    if shell == 'bash':
        say(f'''# Omni AI CLI bash completion. Install:  ai --completions bash > ~/.ai-completion.bash
#   then add to ~/.bashrc:  source ~/.ai-completion.bash
_ai_complete() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  COMPREPLY=( $(compgen -W "{words}" -- "$cur") )
}}
complete -F _ai_complete ai''')
        return 0
    if shell == 'zsh':
        say(f'''#compdef ai
# Omni AI CLI zsh completion. Install:  ai --completions zsh > ~/.zsh/_ai
#   then ensure that dir is on $fpath before compinit in ~/.zshrc
_ai() {{ compadd -- {words} }}
compdef _ai ai''')
        return 0
    say(f'{RED}✗ Shell não suportado: {shell}. Use zsh ou bash.{NC}')
    return 1


def reset_cooldowns():
    def clear(data):
        for key in list(data):
            if key.endswith('_last_rate_limit'):
                data[key] = ''

    config_tool.update_json(STATE_FILE, clear)
    say(f'{GREEN}✓ Cooldowns resetados para todos os perfis.{NC}')


def show_log():
    if not LOG_FILE.is_file():
        say(f'{DIM}Nenhum log encontrado ainda.{NC}')
        return
    say(f'{BOLD}Últimas 20 entradas do log:{NC}\n')
    say('\n'.join(LOG_FILE.read_text(errors='replace').splitlines()[-20:]))


def update():
    before = whats_new.installed_version()
    code = _update()
    if code == 0:
        _restart_service()
        for line in whats_new.render(before, whats_new.installed_version()):
            say(line)
    return code


def _restart_service():
    from ..server import service
    restarted = service.after_update()
    if restarted is not None:
        say(f'{GREEN}✓ Serviço reiniciado.{NC}' if restarted else f'{RED}✗ Reinicie o serviço: ai serve --install-service{NC}')


def _fetch_tags():
    """The version comes from the latest tag: a pull doesn't bring new ones, and the first install is shallow."""
    shallow = subprocess.run(['git', '-C', str(REPO), 'rev-parse', '--is-shallow-repository'],
                             capture_output=True, text=True).stdout.strip() == 'true'
    subprocess.run(['git', '-C', str(REPO), 'fetch', '-q', '--tags', *(['--unshallow'] if shallow else []), 'origin'],
                   capture_output=True, text=True)


def _update():
    """Pull the latest version into the install folder; settings live in ~/.config and are kept."""
    if (REPO / '.git').is_dir() and resolve_bin('git'):
        # Pull the current branch from origin explicitly, so it works even when the branch has no
        # upstream tracking set (a plain `git pull` fails there). A detached HEAD updates from main.
        branch = subprocess.run(['git', '-C', str(REPO), 'rev-parse', '--abbrev-ref', 'HEAD'],
                                capture_output=True, text=True).stdout.strip()
        ref = branch if branch and branch != 'HEAD' else 'main'
        code = subprocess.call(['git', '-C', str(REPO), 'pull', '--ff-only', 'origin', ref])
        if code == 0:
            _fetch_tags()
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
    say('Com o serviço instalado, reinicie depois: ai serve --install-service')
    return 1
