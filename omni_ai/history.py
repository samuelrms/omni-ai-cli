"""Unified, read-only view of past conversations across tools.

Each tool stores its chats in its own place and format; this module discovers them and normalizes
them to one shape so the CLI (`ai history`) and the editor extension can show them together:

    {'id': 'omni:1b0b...', 'source': 'omni', 'title': str, 'updated': float (epoch),
     'cwd': str, 'count': int, 'messages': [{'role': 'user'|'assistant', 'content': str}]}

Readers are defensive: a malformed file is skipped, never fatal. `sessions()` returns metadata only
(no messages) for a fast list; `conversation(id)` loads one in full.
"""
import glob
import json
import os
from pathlib import Path


def _first_line(text, limit=80):
    line = ' '.join(str(text).split())
    return line[:limit - 1] + '…' if len(line) > limit else line


def _dir(env, default):
    return Path(os.environ.get(env, default)).expanduser()


# ─── Omni ────────────────────────────────────────────────────────

def _omni_dir():
    env = os.environ.get('SMART_AI_SESSIONS')
    return Path(env).expanduser() if env else _dir('SMART_AI_DATA', '~/.local/share/smart-ai') / 'sessions'


def _omni_messages(data):
    out = []
    for m in data.get('messages', []):
        role = 'assistant' if m.get('role') == 'assistant' else 'user'
        content = m.get('content')
        if isinstance(content, str) and content.strip():
            out.append({'role': role, 'content': content})
    return out


def _read_omni():
    for path in glob.glob(str(_omni_dir() / '*.json')):
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            continue
        messages = _omni_messages(data)
        if not messages:
            continue
        yield {'id': 'omni:' + data.get('id', Path(path).stem), 'source': 'omni',
               'title': _first_line(messages[0]['content']), 'updated': data.get('updated') or os.path.getmtime(path),
               'cwd': data.get('cwd', ''), 'messages': messages}


# ─── Claude Code ─────────────────────────────────────────────────

def _claude_text(message):
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text', '') for b in content
                         if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def _read_claude():
    root = _dir('CLAUDE_CONFIG_DIR', '~/.claude') / 'projects'
    for path in glob.glob(str(root / '*' / '*.jsonl')):
        messages, cwd = [], ''
        try:
            for raw in Path(path).read_text(errors='replace').splitlines():
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                cwd = obj.get('cwd', cwd)
                if obj.get('type') in ('user', 'assistant') and isinstance(obj.get('message'), dict):
                    text = _claude_text(obj['message'])
                    if text.strip():
                        messages.append({'role': obj['type'], 'content': text})
        except OSError:
            continue
        if not messages:
            continue
        yield {'id': 'claude:' + Path(path).stem, 'source': 'claude',
               'title': _first_line(messages[0]['content']), 'updated': os.path.getmtime(path),
               'cwd': cwd, 'messages': messages}


# ─── Codex ───────────────────────────────────────────────────────

def _codex_text(payload):
    content = payload.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(b.get('text') or b.get('content') or '')
            elif isinstance(b, str):
                parts.append(b)
        return '\n'.join(p for p in parts if p)
    return payload.get('text', '') if isinstance(payload.get('text'), str) else ''


def _read_codex():
    root = _dir('CODEX_HOME', '~/.codex') / 'sessions'
    for path in glob.glob(str(root / '**' / '*.jsonl'), recursive=True):
        messages, cwd = [], ''
        try:
            for raw in Path(path).read_text(errors='replace').splitlines():
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                payload = obj.get('payload', obj)
                if obj.get('type') == 'session_meta' or payload.get('type') == 'session_meta':
                    cwd = payload.get('cwd', cwd)
                role = payload.get('role')
                if role in ('user', 'assistant'):
                    text = _codex_text(payload)
                    if text.strip():
                        messages.append({'role': role, 'content': text})
        except OSError:
            continue
        if not messages:
            continue
        yield {'id': 'codex:' + Path(path).stem, 'source': 'codex',
               'title': _first_line(messages[0]['content']), 'updated': os.path.getmtime(path),
               'cwd': cwd, 'messages': messages}


READERS = {'omni': _read_omni, 'claude': _read_claude, 'codex': _read_codex}


def conversations(sources=None):
    """All conversations (with messages), newest first."""
    picked = sources or list(READERS)
    items = []
    for name in picked:
        reader = READERS.get(name)
        if not reader:
            continue
        try:
            items.extend(reader())
        except Exception:  # a broken tool directory must not sink the rest
            continue
    items.sort(key=lambda c: c.get('updated') or 0, reverse=True)
    return items


def sessions(sources=None):
    """Metadata only (no messages), newest first — for a fast list."""
    return [{k: v for k, v in c.items() if k != 'messages'} | {'count': len(c['messages'])}
            for c in conversations(sources)]


def conversation(conv_id):
    """One conversation in full, by its id, or None."""
    return next((c for c in conversations() if c['id'] == conv_id), None)
