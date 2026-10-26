#!/usr/bin/env python3
"""Share MCP servers and skills between Claude Code, Codex and Antigravity.

  python -m omni_ai.sync            show what is missing where, ask, then apply
  python -m omni_ai.sync --dry-run  only show
  python -m omni_ai.sync --yes      apply without asking

Each CLI keeps its own config; servers are added through each CLI's own
`mcp add` command, and skills are shared by symlink, so nothing is copied twice.
"""
import json
import os
import re
from pathlib import Path
import subprocess
import sys

from .osutils import link, module_cmd, module_env, resolve_bin

HOME = Path(os.environ.get('SMART_AI_SYNC_HOME', '~')).expanduser()
BINS = {'claude': os.environ.get('CLAUDE_BIN', 'claude'), 'codex': os.environ.get('CODEX_BIN', 'codex'),
        'agy': os.environ.get('AGY_BIN', 'agy')}
# Where each CLI looks for user-level skills (first entry is where new links go)
SKILL_DIRS = {
    'claude': [HOME / '.claude' / 'skills'],
    'codex': [HOME / '.agents' / 'skills', HOME / '.codex' / 'skills'],
    'agy': [HOME / '.gemini' / 'antigravity' / 'skills'],
}


def installed(tool):
    return resolve_bin(BINS[tool]) is not None


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def normalize(raw):
    """Common shape: {'url', 'headers'} for http, {'command', 'args', 'env'} for stdio."""
    url = raw.get('url') or raw.get('serverUrl')
    if url:
        return {'url': url, 'headers': raw.get('headers') or raw.get('http_headers') or {}}
    if raw.get('command'):
        return {'command': raw['command'], 'args': list(raw.get('args') or []), 'env': dict(raw.get('env') or {})}
    return None


def servers_claude():
    return {name: normalize(conf) for name, conf in read_json(HOME / '.claude.json').get('mcpServers', {}).items()}


def servers_codex():
    # config.toml only: `codex mcp list` also shows plugin-bundled servers that don't work outside Codex
    import tomllib
    try:
        data = tomllib.loads((HOME / '.codex' / 'config.toml').read_text())
    except (OSError, ValueError):
        return {}
    return {name: normalize(conf) for name, conf in data.get('mcp_servers', {}).items()}


RUNNERS = {'npx', 'uvx', 'bunx', 'pipx', 'pnpx', 'docker', 'node', 'python', 'python3', 'uv'}


def identity(conf):
    """Same server under another name: compare what actually runs (URL, or the package/binary)."""
    if 'url' in conf:
        return conf['url'].rstrip('/')
    program = Path(conf['command']).name
    if program in RUNNERS:
        program = next((a for a in conf['args'] if not a.startswith('-') and a not in ('run', 'exec')), program)
    program = re.sub(r'@[\d.]+$', '', program.split('/')[-1])
    return re.sub(r'^(mcp-server-|server-|mcp-)|(-mcp|-server)$', '', program)


def app_internal(conf):
    """Servers that ship inside a desktop app (e.g. Codex.app) only work in that app."""
    command = conf.get('command', '')
    return '.app/' in command or command.startswith('./')


def servers_agy():
    return {name: normalize(conf) for name, conf in
            read_json(HOME / '.gemini' / 'config' / 'mcp_config.json').get('mcpServers', {}).items()}


READERS = {'claude': servers_claude, 'codex': servers_codex, 'agy': servers_agy}


def add_command(tool, name, conf):
    """Argv that registers one server in one CLI, or None when that CLI can't express it."""
    if 'url' in conf:
        if tool == 'claude':
            return [BINS[tool], 'mcp', 'add-json', '-s', 'user', name,
                    json.dumps({'type': 'http', 'url': conf['url'], 'headers': conf['headers']})]
        if tool == 'codex':
            return None if conf['headers'] else [BINS[tool], 'mcp', 'add', name, '--url', conf['url']]
        return [BINS[tool], 'mcp', 'add'] + [f for k, v in conf['headers'].items() for f in ('-H', f'{k}: {v}')] \
            + [name, conf['url']]
    if tool == 'claude':
        return [BINS[tool], 'mcp', 'add-json', '-s', 'user', name,
                json.dumps({'type': 'stdio', 'command': conf['command'], 'args': conf['args'], 'env': conf['env']})]
    flag = '--env' if tool == 'codex' else '-e'
    env = [f for k, v in conf['env'].items() for f in (flag, f'{k}={v}')]
    return [BINS[tool], 'mcp', 'add'] + env + [name, '--', conf['command']] + conf['args']


def skills_by_tool():
    found = {}
    for tool, dirs in SKILL_DIRS.items():
        found[tool] = {}
        for folder in dirs:
            if folder.is_dir():
                for skill in folder.iterdir():
                    if not skill.name.startswith('.') and (skill / 'SKILL.md').is_file():
                        found[tool].setdefault(skill.name, skill.resolve())
    return found


def plan():
    tools = [tool for tool in BINS if installed(tool)]
    servers = {tool: {k: v for k, v in READERS[tool]().items() if v and not app_internal(v)} for tool in tools}
    union = {}
    for tool in tools:
        for name, conf in servers[tool].items():
            union.setdefault(name, (tool, conf))
    mcp_actions, skipped = [], []
    known = {tool: {identity(conf) for conf in servers[tool].values()} for tool in tools}
    for name, (origin, conf) in sorted(union.items()):
        for tool in tools:
            if name not in servers[tool] and identity(conf) not in known[tool]:
                known[tool].add(identity(conf))
                argv = add_command(tool, name, conf)
                (mcp_actions if argv else skipped).append((tool, name, origin, argv))
    skills = skills_by_tool()
    all_skills = {}
    for tool in tools:
        for name, path in skills.get(tool, {}).items():
            all_skills.setdefault(name, (tool, path))
    skill_actions = []
    for name, (origin, path) in sorted(all_skills.items()):
        for tool in tools:
            if name not in skills.get(tool, {}):
                skill_actions.append((tool, name, origin, path, SKILL_DIRS[tool][0] / name))
    return tools, mcp_actions, skipped, skill_actions


def main(argv):
    dry, yes = '--dry-run' in argv, '--yes' in argv
    tools, mcp_actions, skipped, skill_actions = plan()
    print('Ferramentas encontradas: ' + ', '.join(tools))
    if not mcp_actions and not skill_actions:
        print('✓ Tudo sincronizado: as três ferramentas já têm os mesmos MCPs e skills.')
        return 0
    for tool, name, origin, _ in mcp_actions:
        print(f'  MCP   {name:<24} {origin} → {tool}')
    for tool, name, origin, _, target in skill_actions:
        print(f'  skill {name:<24} {origin} → {tool}  ({target})')
    for tool, name, origin, _ in skipped:
        print(f'  pula  {name:<24} {origin} → {tool}: servidor HTTP com cabeçalhos não é suportado pelo {tool}')
    if any(tool != 'claude' and any(a in ('--env', '-e') for a in argv) or tool == 'claude' and '"env": {}' not in argv[-1]
           for tool, _, _, argv in mcp_actions if 'url' not in argv[-1]):
        print('Atenção: variáveis de ambiente dos MCPs (podem conter tokens) serão copiadas para a config da outra ferramenta.')
    if dry:
        return 0
    if not yes:
        try:
            if input('Aplicar? [s/N] ').strip().lower() not in ('s', 'sim', 'y', 'yes', 'o', 'oui'):
                print('Nada alterado.')
                return 0
        except (EOFError, KeyboardInterrupt):
            print('\nNada alterado.')
            return 0
    failures = 0
    for tool, name, _, argv in mcp_actions:
        result = subprocess.run([resolve_bin(argv[0]) or argv[0]] + argv[1:], capture_output=True, text=True)
        ok = result.returncode == 0
        failures += not ok
        print(f"  {'✓' if ok else '✗'} MCP {name} em {tool}" + ('' if ok else f': {(result.stderr or result.stdout).strip()[:200]}'))
    for tool, name, _, path, target in skill_actions:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            link(target, path)
            print(f'  ✓ skill {name} em {tool}')
        except OSError as error:
            failures += 1
            print(f'  ✗ skill {name} em {tool}: {error}')
    # Extra Claude accounts read their own folder; refresh them with the new servers and skills
    subprocess.run(module_cmd('config') + ['account', 'sync-all'], env=module_env())
    print('Reinicie sessões abertas dos CLIs para carregar o que foi adicionado.')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
