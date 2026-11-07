#!/usr/bin/env python3
"""Export a conversation to PDF or a Markdown handoff, with no external dependencies.

Split into submodules: convert (data + Markdown + clipboard), pdf. Public names
are re-exported here so omni_ai.export.<name> keeps working.
"""
import json
import os
import sys

from ..osutils import private_file
from .convert import (normalize, _when, default_name, _role_label, to_markdown, to_clipboard)
from .pdf import to_pdf, _esc, _wrap, _layout, _paginate, _content_stream

def _load(from_path):
    if from_path:
        with open(from_path, encoding='utf-8') as handle:
            raw = handle.read()
    else:
        raw = sys.stdin.read()
    return normalize(json.loads(raw))


def main(argv):
    mode = argv[0] if argv else ''
    rest = argv[1:]
    model, from_path, out_path = None, None, None
    positional = []
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg in ('--from', '--out'):
            if i + 1 >= len(rest):
                print(f'{arg} precisa de um valor.', file=sys.stderr)
                return 2
            if arg == '--from':
                from_path = rest[i + 1]
            else:
                out_path = rest[i + 1]
            i += 2; continue
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
        with private_file(out_path, 'w', encoding='utf-8') as stream:
            stream.write(text)
        copied = to_clipboard(text)
        print(os.path.abspath(out_path))
        if copied:
            print('(copiado para a área de transferência)', file=sys.stderr)
        return 0
    print('Uso: export (--print | --export [model]) [--from conv.json] [--out arquivo]', file=sys.stderr)
    return 2
