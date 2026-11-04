#!/usr/bin/env python3
"""
Exporta uma conversa do Omni AI em dois formatos, sem dependências externas:

- PDF (``to_pdf``): um documento paginado para arquivar ou mandar para o time de
  análise. Usa só as fontes base-14 do PDF (Helvetica / Courier), então não
  precisa embutir nada nem instalar bibliotecas.
- Markdown de handoff (``to_markdown``): um "copy" com o resumo e o histórico da
  conversa para continuar em outro lugar, com outra LLM.

Entrada (``normalize``): aceita o ``session.data`` do chat (dict com ``messages``)
ou uma lista crua de mensagens ``[{'role','content','provider'}]``.

CLI (``main``):
    ai --print  --from conv.json --out conversa.pdf
    ai --export [model] --from conv.json --out conversa.md
A conversa também pode vir pelo stdin quando ``--from`` é omitido.
"""

import datetime
import json
import os
import re
import sys
import time


# --------------------------------------------------------------------------- data

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
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M')
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
# tipograficamente perfeito, só legível.
_PAGE_W, _PAGE_H = 612.0, 792.0
_MARGIN = 54.0
_SIZE = 10.0
_LEADING = 14.0
_HELV_AVG = 0.52   # largura média de char em Helvetica, fração do tamanho
_COUR_W = 0.60     # Courier é monoespaçada


def _esc(text):
    return text.replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)')


def _wrap(text, mono, usable):
    """Quebra uma linha lógica em linhas físicas que cabem na largura útil."""
    per_char = (_COUR_W if mono else _HELV_AVG) * _SIZE
    limit = max(8, int(usable / per_char))
    text = text.replace('\t', '    ')
    if len(text) <= limit:
        return [text]
    out, words = [], text.split(' ')
    cur = ''
    for word in words:
        while len(word) > limit:            # palavra maior que a linha: corta no braço
            if cur:
                out.append(cur)
                cur = ''
            out.append(word[:limit])
            word = word[limit:]
        candidate = word if not cur else cur + ' ' + word
        if len(candidate) > limit:
            out.append(cur)
            cur = word
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out or ['']


def _layout(conv):
    """Transforma a conversa numa lista de linhas (texto, fonte) já quebradas."""
    usable = _PAGE_W - 2 * _MARGIN
    rows = []

    def add(text, font='H', blank_after=False):
        mono = font == 'C'
        for physical in (_wrap(text, mono, usable) if text else ['']):
            rows.append((physical, 'HB' if font == 'HB' else ('C' if mono else 'H')))
        if blank_after:
            rows.append(('', 'H'))

    add(conv['title'], 'HB')
    meta = []
    if conv.get('id'):
        meta.append('sessão ' + conv['id'])
    meta.append(_when(conv.get('created')))
    if conv.get('cwd'):
        meta.append(conv['cwd'])
    add(' · '.join(meta), 'H', blank_after=True)
    add(f"{len(conv['messages'])} mensagens", 'H', blank_after=True)

    for message in conv['messages']:
        if message['role'] == 'changes':
            label = _role_label('changes') + ': ' + message['content']
            add(label, 'HB', blank_after=True)
            continue
        who = _role_label(message['role'])
        if message['provider']:
            who += f" ({message['provider']})"
        add(who, 'HB')
        mono = False
        for raw in message['content'].split('\n'):
            if raw.strip().startswith('```'):
                mono = not mono
                continue
            add(raw, 'C' if mono else 'H')
        add('', 'H')
    return rows


def _paginate(rows):
    """Agrupa as linhas em páginas conforme a altura útil."""
    top = _PAGE_H - _MARGIN
    bottom = _MARGIN
    per_page = int((top - bottom) / _LEADING)
    return [rows[i:i + per_page] for i in range(0, len(rows), per_page)] or [[]]


def _content_stream(page_rows):
    parts = ['BT', f'{_LEADING} TL', f'1 0 0 1 {_MARGIN} {_PAGE_H - _MARGIN} Tm']
    font = None
    for text, kind in page_rows:
        want = {'HB': '/F2', 'C': '/F3'}.get(kind, '/F1')
        if want != font:
            parts.append(f'{want} {int(_SIZE)} Tf')
            font = want
        parts.append(f'({_esc(text)}) Tj')
        parts.append('T*')
    parts.append('ET')
    return '\n'.join(parts).encode('cp1252', 'replace')


def to_pdf(conv, out_path):
    """Escreve a conversa como PDF em ``out_path``. Retorna o caminho."""
    pages = _paginate(_layout(conv))
    objects = []            # bytes de cada objeto, na ordem dos números

    def obj(data):
        objects.append(data)
        return len(objects)   # número 1-based

    # Fontes base-14.
    enc = b' /Encoding /WinAnsiEncoding >>'
    f1 = obj(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica' + enc)
    f2 = obj(b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold' + enc)
    f3 = obj(b'<< /Type /Font /Subtype /Type1 /BaseFont /Courier' + enc)
    resources = (f'<< /Font << /F1 {f1} 0 R /F2 {f2} 0 R /F3 {f3} 0 R >> >>').encode()

    pages_id = len(objects) + 1          # reservamos o número do nó /Pages
    objects.append(None)                 # placeholder, preenchido no fim

    page_ids = []
    for page_rows in pages:
        stream = _content_stream(page_rows)
        content_id = obj(b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream')
        page_id = obj((f'<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {int(_PAGE_W)} {int(_PAGE_H)}] '
                       f'/Resources {resources.decode()} /Contents {content_id} 0 R >>').encode())
        page_ids.append(page_id)

    kids = ' '.join(f'{pid} 0 R' for pid in page_ids)
    objects[pages_id - 1] = (f'<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>').encode()
    catalog_id = obj((f'<< /Type /Catalog /Pages {pages_id} 0 R >>').encode())

    # Monta o arquivo com a tabela xref.
    out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f'{number} 0 obj\n'.encode() + body + b'\nendobj\n'
    xref_at = len(out)
    out += f'xref\n0 {len(objects) + 1}\n'.encode()
    out += b'0000000000 65535 f \n'
    for off in offsets[1:]:
        out += f'{off:010d} 00000 n \n'.encode()
    out += (f'trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n'
            f'startxref\n{xref_at}\n%%EOF\n').encode()

    with open(out_path, 'wb') as stream:
        stream.write(out)
    return out_path


# --------------------------------------------------------------------------- clipboard (best effort)

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

def _load(from_path):
    raw = open(from_path, encoding='utf-8').read() if from_path else sys.stdin.read()
    return normalize(json.loads(raw))


def main(argv):
    mode = argv[0] if argv else ''
    rest = argv[1:]
    model, from_path, out_path = None, None, None
    positional = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == '--from':
            from_path = rest[i + 1]; i += 2; continue
        if arg == '--out':
            out_path = rest[i + 1]; i += 2; continue
        positional.append(arg); i += 1
    if mode == '--export' and positional:
        model = positional[0]

    try:
        conv = _load(from_path)
    except (OSError, ValueError) as error:
        print(f'Não foi possível ler a conversa: {error}', file=sys.stderr)
        return 1

    if mode == '--print':
        out_path = out_path or default_name(conv, 'pdf')
        to_pdf(conv, out_path)
        print(os.path.abspath(out_path))
        return 0
    if mode == '--export':
        text = to_markdown(conv, model)
        out_path = out_path or default_name(conv, 'md')
        with open(out_path, 'w', encoding='utf-8') as stream:
            stream.write(text)
        copied = to_clipboard(text)
        print(os.path.abspath(out_path))
        if copied:
            print('(copiado para a área de transferência)', file=sys.stderr)
        return 0
    print('Uso: export (--print | --export [model]) [--from conv.json] [--out arquivo]', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
