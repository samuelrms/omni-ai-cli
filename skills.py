"""Slash commands and skills each CLI knows about, from the project and from the user's folders."""
import os
from pathlib import Path
import re

HOME = Path(os.environ.get('SMART_AI_SYNC_HOME', '~')).expanduser()
PREFERENCE = ('claude', 'codex', 'agy')


def _sources(cwd):
    cwd = Path(cwd)
    # (tool, kind, folder): kind 'command' = one .md per command, 'skill' = folder with SKILL.md
    return [
        ('claude', 'command', cwd / '.claude' / 'commands'), ('claude', 'skill', cwd / '.claude' / 'skills'),
        ('claude', 'command', HOME / '.claude' / 'commands'), ('claude', 'skill', HOME / '.claude' / 'skills'),
        ('codex', 'skill', cwd / '.agents' / 'skills'), ('codex', 'skill', HOME / '.agents' / 'skills'),
        ('codex', 'skill', HOME / '.codex' / 'skills'), ('codex', 'command', HOME / '.codex' / 'prompts'),
        ('agy', 'skill', cwd / '.agents' / 'skills'), ('agy', 'command', cwd / '.agent' / 'workflows'),
        ('agy', 'skill', HOME / '.gemini' / 'antigravity' / 'skills'),
        ('agy', 'command', HOME / '.gemini' / 'antigravity' / 'global_workflows'),
    ]


def discover(cwd):
    """{name: {tool: (kind, path, scope)}}; project entries win over user ones with the same name."""
    found = {}
    for tool, kind, folder in _sources(cwd):
        if not folder.is_dir():
            continue
        scope = 'projeto' if str(folder).startswith(str(Path(cwd))) and Path(cwd) != HOME else 'usuário'
        if kind == 'command':
            entries = [(p.relative_to(folder).with_suffix('').as_posix().replace('/', ':'), p)
                       for p in folder.rglob('*.md')]
        else:
            entries = [(p.name, p / 'SKILL.md') for p in folder.iterdir()
                       if not p.name.startswith('.') and (p / 'SKILL.md').is_file()]
        for name, path in entries:
            found.setdefault(name, {}).setdefault(tool, (kind, path, scope))
    return found


def description(path):
    try:
        text = Path(path).read_text(errors='replace')
    except OSError:
        return ''
    match = re.search(r'^description:[ \t]*(.*)$', text, re.M)
    if match:
        value = match.group(1).strip()
        if value in ('', '>', '>-', '|', '|-'):
            # YAML block scalar: the text is on the following indented lines
            lines = re.match(r'((?:[ \t]+.*\n?)*)', text[match.end() + 1:]).group(1).split()
            value = ' '.join(lines)
        return value.strip('"\'')
    body = re.sub(r'^---.*?---\s*', '', text, flags=re.S).strip().splitlines()
    return body[0].lstrip('# ').strip() if body else ''


def pick_tool(entry, engine):
    if engine in entry:
        return engine
    return next(tool for tool in PREFERENCE if tool in entry)


def build_prompt(name, args, tool, entry):
    """Claude runs its own slash commands natively; others get the instructions inlined."""
    kind, path, _ = entry[tool]
    if tool == 'claude':
        return f'/{name} {args}'.strip(), True
    text = Path(path).read_text(errors='replace')
    if kind == 'command':
        text = re.sub(r'^---.*?---\s*', '', text, flags=re.S)
        parts = args.split()
        text = text.replace('$ARGUMENTS', args)
        for i, part in enumerate(parts, 1):
            text = text.replace(f'${i}', part)
        return text + ('' if '$ARGUMENTS' in Path(path).read_text(errors='replace') or not args
                       else f'\n\nArgumentos: {args}'), False
    return (f'Use a skill "{name}" definida em {path}. Leia esse arquivo e siga as instruções dele.'
            + (f'\n\nPedido: {args}' if args else '')), False
