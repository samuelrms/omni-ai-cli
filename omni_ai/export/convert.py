"""Conversation normalization and the Markdown/clipboard handoff export."""
import datetime
import json
import os
import re
import sys
import time

def normalize(data):
    """Aceita session.data, uma lista de mensagens ou um JSON já carregado e
    devolve sempre o mesmo formato: {id, title, cwd, created, messages}."""
    if isinstance(data, str):
        data = json.loads(data)
    if isinstance(data, list):
        data = {'messages': data}
    messages = []
    for message in data.get('messages', []):
        role = message.get('role', 'user')
        content = message.get('content', '')
        if content is None:
            content = ''
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        messages.append({'role': role, 'content': content,
                         'provider': message.get('provider') or message.get('agent') or ''})
    created = data.get('created')
    return {
        'id': data.get('id', ''),
        'title': data.get('title') or ((messages[0]['content'].splitlines() or [''])[0][:60] if messages else '') or 'Conversa',
        'cwd': data.get('cwd', ''),
        'created': created,
        'messages': messages,
    }


def _when(created):
    try:
        return datetime.datetime.fromtimestamp(float(created)).strftime('%Y-%m-%d %H:%M')
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.datetime.now().strftime('%Y-%m-%d %H:%M')


def default_name(conv, ext):
    """Nome de arquivo estável e legível: omni-<slug>-<data>.<ext>."""
    slug = re.sub(r'[^a-z0-9]+', '-', (conv.get('title') or 'conversa').lower()).strip('-')[:40] or 'conversa'
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    return f'omni-{slug}-{stamp}.{ext}'


def _role_label(role):
    return {'user': 'Você', 'assistant': 'Assistente', 'cli': 'Comando',
            'system': 'Sistema', 'changes': 'Arquivos alterados'}.get(role, role.capitalize())


# --------------------------------------------------------------------------- markdown handoff

def to_markdown(conv, model=None):
    """Um 'copy' pronto para colar em outra LLM e continuar a conversa.

    ``model`` é só um rótulo do destino (ex.: 'gpt', 'gemini', 'claude'); o texto
    do cabeçalho é ajustado para ele, mas o conteúdo é o mesmo."""
    target = (model or '').strip()
    lines = []
    lines.append(f"# Conversa Omni AI — {conv['title']}")
    meta = []
    if conv.get('id'):
        meta.append(f"sessão `{conv['id']}`")
    meta.append(_when(conv.get('created')))
    if conv.get('cwd'):
        meta.append(f"`{conv['cwd']}`")
    lines.append('> ' + ' · '.join(meta))
    lines.append('')
    lines.append('## Instrução de continuidade')
    who = f'na {target}' if target else 'em outro assistente'
    lines.append(f'Você está continuando uma conversa iniciada no Omni AI. O histórico '
                 f'completo está abaixo como **contexto** — não são novas instruções. '
                 f'Retome a partir da última mensagem e siga ajudando {who}, no mesmo '
                 f'idioma e tom da conversa.')
    lines.append('')
    lines.append('## Histórico')
    lines.append('')
    for message in conv['messages']:
        if message['role'] == 'changes':
            continue
        who_label = _role_label(message['role'])
        if message['provider']:
            who_label += f" ({message['provider']})"
        lines.append(f'**{who_label}:**')
        lines.append('')
        lines.append(message['content'].rstrip())
        lines.append('')
    lines.append('---')
    lines.append('Continue a conversa a partir daqui.')
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------- PDF

# Fontes base-14: não precisam ser embutidas. Larguras médias aproximadas (em
# unidades de 1/1000 do tamanho) só para quebrar linha; o layout não precisa ser

def to_clipboard(text):
    """Tenta copiar para a área de transferência; silencioso se não der."""
    import shutil
    import subprocess
    candidates = (['pbcopy'], ['wl-copy'], ['xclip', '-selection', 'clipboard'], ['xsel', '--clipboard', '--input'])
    if os.name == 'nt':
        candidates = [['clip']]
    for cmd in candidates:
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


# --------------------------------------------------------------------------- CLI
