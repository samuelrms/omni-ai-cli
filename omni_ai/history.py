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


def _num(value, default):
    """A usable epoch number; hand-edited or wrong-typed values fall back instead of crashing the sort."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _omni_messages(data):
    out = []
    items = data.get('messages', []) if isinstance(data, dict) else []
    for m in items if isinstance(items, list) else []:
        if not isinstance(m, dict):
            continue
        role = 'assistant' if m.get('role') == 'assistant' else 'user'
        content = m.get('content')
        if isinstance(content, str) and content.strip():
            out.append({'role': role, 'content': content})
    return out


def _each(pattern, read, recursive=False):
    """One broken file must not hide every other conversation of the tool."""
    for path in glob.glob(pattern, recursive=recursive):
        try:
            item = read(path)
        except Exception:
            continue
        if item:
            yield item


def _lines(path):
    """The JSON objects of a log. Reading a line that makes no sense is the caller's to skip."""
    for raw in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def _omni_file(path):
    data = json.loads(Path(path).read_text(encoding='utf-8', errors='replace'))
    if not isinstance(data, dict):
        return None
    messages = _omni_messages(data)
    if not messages:
        return None
    return {'id': 'omni:' + str(data.get('id') or Path(path).stem), 'source': 'omni',
            'title': _first_line(messages[0]['content']),
            'updated': _num(data.get('updated'), os.path.getmtime(path)),
            'cwd': data.get('cwd', '') if isinstance(data.get('cwd'), str) else '', 'messages': messages}


def _read_omni():
    return _each(str(_omni_dir() / '*.json'), _omni_file)


# ─── Claude Code ─────────────────────────────────────────────────

def _claude_text(message):
    content = message.get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text', '') for b in content
                         if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def _claude_file(path):
    messages, cwd = [], ''
    for obj in _lines(path):
        try:
            cwd = obj['cwd'] if isinstance(obj.get('cwd'), str) else cwd
            if obj.get('type') in ('user', 'assistant') and isinstance(obj.get('message'), dict):
                text = _claude_text(obj['message'])
                if text.strip():
                    messages.append({'role': obj['type'], 'content': text})
        except (AttributeError, TypeError):
            continue
    if not messages:
        return None
    return {'id': 'claude:' + Path(path).stem, 'source': 'claude',
            'title': _first_line(messages[0]['content']), 'updated': os.path.getmtime(path),
            'cwd': cwd, 'messages': messages}


def _read_claude():
    return _each(str(_dir('CLAUDE_CONFIG_DIR', '~/.claude') / 'projects' / '*' / '*.jsonl'), _claude_file)


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


def _codex_file(path):
    messages, cwd = [], ''
    for obj in _lines(path):
        try:
            payload = obj.get('payload', obj)
            if not isinstance(payload, dict):
                continue
            if (obj.get('type') == 'session_meta' or payload.get('type') == 'session_meta') and isinstance(payload.get('cwd'), str):
                cwd = payload['cwd']
            role = payload.get('role')
            if role in ('user', 'assistant'):
                text = _codex_text(payload)
                if text.strip():
                    messages.append({'role': role, 'content': text})
        except (AttributeError, TypeError):
            continue
    if not messages:
        return None
    return {'id': 'codex:' + Path(path).stem, 'source': 'codex',
            'title': _first_line(messages[0]['content']), 'updated': os.path.getmtime(path),
            'cwd': cwd, 'messages': messages}


def _read_codex():
    return _each(str(_dir('CODEX_HOME', '~/.codex') / 'sessions' / '**' / '*.jsonl'), _codex_file, recursive=True)


# ─── Cursor ──────────────────────────────────────────────────────
# Cursor keeps chats in a SQLite "state" DB: composerData:<id> holds a conversation's metadata and the
# ordered bubble headers; bubbleId:<composerId>:<bubbleId> holds each message (type 1=user, 2=assistant).
# Listing reads only composerData (fast); the bubbles are fetched only when one conversation is opened.

def _cursor_db():
    env = os.environ.get('CURSOR_STATE_DB')
    if env:
        return Path(env).expanduser()
    for candidate in ('~/Library/Application Support/Cursor/User/globalStorage/state.vscdb',
                      '~/.config/Cursor/User/globalStorage/state.vscdb',
                      '~/AppData/Roaming/Cursor/User/globalStorage/state.vscdb'):
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def _cursor_connect():
    import sqlite3
    db = _cursor_db()
    if not db or not db.is_file():
        return None
    try:
        return sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    except Exception:
        return None


def _cursor_sessions():
    con = _cursor_connect()
    if not con:
        return
    try:
        for (value,) in con.execute("SELECT value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
            try:
                d = json.loads(value)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            try:
                heads = d.get('fullConversationHeadersOnly') or []
                name = d.get('name') or (d.get('text') or '').strip()
                if not name or not heads:
                    continue
                item = {'id': 'cursor:' + str(d.get('composerId', '')), 'source': 'cursor',
                        'title': _first_line(name),
                        'updated': _num(d.get('lastUpdatedAt') or d.get('createdAt'), 0) / 1000,
                        'cwd': '', 'count': len(heads)}
            except (AttributeError, TypeError):
                continue
            yield item
    finally:
        con.close()


def _cursor_load(conv_id):
    cid = conv_id.split(':', 1)[1]
    con = _cursor_connect()
    if not con:
        return None
    try:
        row = con.execute("SELECT value FROM cursorDiskKV WHERE key=?", (f'composerData:{cid}',)).fetchone()
        if not row:
            return None
        d = json.loads(row[0])
        if not isinstance(d, dict):
            return None
        messages = []
        for head in d.get('fullConversationHeadersOnly') or []:
            if not isinstance(head, dict):
                continue
            bid = head.get('bubbleId')
            br = con.execute("SELECT value FROM cursorDiskKV WHERE key=?", (f'bubbleId:{cid}:{bid}',)).fetchone()
            if not br:
                continue
            try:
                b = json.loads(br[0])
            except ValueError:
                continue
            text = b.get('text') if isinstance(b, dict) else ''
            if isinstance(text, str) and text.strip():
                messages.append({'role': 'assistant' if b.get('type') == 2 else 'user', 'content': text})
        if not messages:
            return None
        return {'id': conv_id, 'source': 'cursor', 'title': _first_line(d.get('name') or messages[0]['content']),
                'updated': _num(d.get('lastUpdatedAt') or d.get('createdAt'), 0) / 1000, 'cwd': '', 'messages': messages}
    finally:
        con.close()


READERS = {'omni': _read_omni, 'claude': _read_claude, 'codex': _read_codex}
# Sources with a fast metadata list separate from the heavy per-conversation load.
LAZY = {'cursor': (_cursor_sessions, _cursor_load)}


ALL_SOURCES = list(READERS) + list(LAZY)


def sessions(sources=None):
    """Metadata only (no messages), newest first — for a fast list across all sources."""
    picked = sources or ALL_SOURCES
    items = []
    for name in picked:
        try:
            if name in READERS:
                items += [{k: v for k, v in c.items() if k != 'messages'} | {'count': len(c['messages'])}
                          for c in READERS[name]()]
            elif name in LAZY:
                items += list(LAZY[name][0]())
        except Exception:  # a broken tool store must not sink the rest
            continue
    items.sort(key=lambda c: _num(c.get('updated'), 0), reverse=True)
    return items


def conversation(conv_id):
    """One conversation in full, by its id, or None."""
    source = conv_id.split(':', 1)[0]
    if source in LAZY:
        try:
            return LAZY[source][1](conv_id)
        except Exception:
            return None
    reader = READERS.get(source)
    if not reader:
        return None
    try:
        return next((c for c in reader() if c['id'] == conv_id), None)
    except Exception:
        return None
