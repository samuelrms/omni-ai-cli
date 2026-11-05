"""Screen text: language, translations, width/truncation, home paths, markdown, fuzzy match, escaping."""
import re
import shutil
from pathlib import Path

from . import color

SECRET = re.compile(r'^\s*/key\b|sk-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{20,}|npm_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}')
LANG = ['pt']  # set by the chat; screen texts follow it
TEXT = {
    'cancel': {'pt': 'Ctrl-C cancela', 'en': 'Ctrl-C cancels', 'es': 'Ctrl-C cancela', 'fr': 'Ctrl-C annule'},
    'no_output': {'pt': '(sem saída)', 'en': '(no output)', 'es': '(sin salida)', 'fr': '(aucune sortie)'},
    'more': {'pt': '+ {n} linhas · /trace para ver tudo', 'en': '+ {n} lines · /trace to see all',
             'es': '+ {n} líneas · /trace para ver todo', 'fr': '+ {n} lignes · /trace pour tout voir'},
    'older': {'pt': '… {n} mensagens anteriores · /history mostra tudo', 'en': '… {n} earlier messages · /history shows all',
              'es': '… {n} mensajes anteriores · /history muestra todo', 'fr': '… {n} messages précédents · /history montre tout'},
    'session': {'pt': 'sessão', 'en': 'session', 'es': 'sesión', 'fr': 'session'},
    'limit': {'pt': 'LIMITE', 'en': 'LIMIT', 'es': 'LÍMITE', 'fr': 'LIMITE'},
    'error': {'pt': 'ERRO', 'en': 'ERROR', 'es': 'ERROR', 'fr': 'ERREUR'},
    'warn': {'pt': 'AVISO', 'en': 'WARNING', 'es': 'AVISO', 'fr': 'AVERTISSEMENT'},
    'file': {'pt': 'arquivo', 'en': 'file', 'es': 'archivo', 'fr': 'fichier'},
    'args': {'pt': 'Tab mostra opções · Enter sem nada pula', 'en': 'Tab shows options · Enter alone skips',
             'es': 'Tab muestra opciones · Enter vacío omite', 'fr': 'Tab montre les options · Entrée seule ignore'},
}


def t(key, **values):
    return TEXT[key].get(LANG[0], TEXT[key]['pt']).format(**values)


def width():
    return max(40, shutil.get_terminal_size((100, 30)).columns)


def short(text, limit):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def home_path(path):
    path = str(path)
    home = str(Path.home())
    return '~' + path[len(home):] if path.startswith(home) else path


def markdown(text):
    """Small terminal markdown: headings, bold, inline code, bullets, fenced code."""
    out, fence = [], False
    for line in text.splitlines():
        if line.strip().startswith('```'):
            fence = not fence
            continue
        if fence:
            out.append(color.dim('  ' + line))
            continue
        heading = re.match(r'^(#{1,6})\s+(.*)', line)
        if heading:
            out.append(color.bold(heading.group(2)))
            continue
        line = re.sub(r'^(\s*)[-*]\s+', lambda m: m.group(1) + '• ', line)
        line = re.sub(r'\*\*(.+?)\*\*', lambda m: color.bold(m.group(1)), line)
        line = re.sub(r'`([^`]+)`', lambda m: color.cyan(m.group(1)), line)
        out.append(line)
    return '\n'.join(out)


def fuzzy(query, text):
    """Rank for 'query' inside 'text': 0 prefix, 1 substring, 2 letters in order, None no match."""
    query, text = query.lower(), text.lower()
    if text.startswith(query):
        return 0
    if query in text:
        return 1
    position = 0
    for char in query:
        position = text.find(char, position) + 1
        if not position:
            return None
    return 2


def escape(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
