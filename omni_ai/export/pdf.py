"""Zero-dependency PDF export using the base-14 fonts (Helvetica / Courier)."""
import re
import sys
import time

from .convert import _role_label, _when

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

    # As fontes base-14 só cobrem Latin-1 (cp1252); qualquer coisa fora disso
    # (emoji, CJK, cirílico…) vira "?" no PDF. Avisa sem interromper.
    lost = set()
    for page_rows in pages:
        for text, _ in page_rows:
            for char in text:
                try:
                    char.encode('cp1252')
                except UnicodeEncodeError:
                    lost.add(char)
    if lost:
        print(f'Aviso: {len(lost)} caractere(s) fora do Latin-1 viraram "?" no PDF '
              f'(as fontes padrão do PDF não os cobrem). Use --export para o texto completo.',
              file=sys.stderr)

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
