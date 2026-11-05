#!/usr/bin/env python3
"""
PII Masker — Mascara dados pessoais/sensíveis antes de enviar para terceiros.

Uso standalone:
    echo "Meu email é foo@bar.com" | python -m omni_ai.pii mask
    echo "Meu email é [EMAIL_1]"   | python -m omni_ai.pii unmask --map map.json

Uso como módulo:
    from omni_ai.pii import PIIMasker  # noqa
    masker = PIIMasker()
    masked, mapping = masker.mask("Meu CPF é 123.456.789-00")
    original = masker.unmask(masked, mapping)
"""

import re
import json
import sys

from .masker import PIIMasker

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Mascara/desmascara PII em texto")
    sub = parser.add_subparsers(dest="command")

    mask_cmd = sub.add_parser("mask", help="Mascara PII do stdin")
    mask_cmd.add_argument("--map-out", "-m", help="Salvar mapa de reversão em arquivo JSON")
    mask_cmd.add_argument("--report", "-r", action="store_true", help="Imprimir relatório de PII encontrado")

    unmask_cmd = sub.add_parser("unmask", help="Desmascara PII do stdin usando mapa")
    unmask_cmd.add_argument("--map", required=True, help="Arquivo JSON com mapa de reversão")

    report_cmd = sub.add_parser("report", help="Apenas relata PII encontrado, sem mascarar")

    args = parser.parse_args()
    masker = PIIMasker()

    if args.command == "mask":
        text = sys.stdin.read()
        masked, mapping = masker.mask(text)
        print(masked, end="")
        if args.map_out:
            with open(args.map_out, "w", encoding="utf-8") as f:
                json.dump(mapping, f, indent=2, ensure_ascii=False)
            print(f"\n[PII Map saved to {args.map_out}]", file=sys.stderr)
        if args.report:
            report = masker.get_report(text)
            print(f"\n[PII Report: {json.dumps(report)}]", file=sys.stderr)

    elif args.command == "unmask":
        text = sys.stdin.read()
        try:
            with open(args.map, "r", encoding="utf-8") as f:
                mapping = json.load(f)
        except (OSError, ValueError) as error:
            print(f"Não foi possível ler o mapa: {error}", file=sys.stderr)
            sys.exit(1)
        print(masker.unmask(text, mapping), end="")

    elif args.command == "report":
        text = sys.stdin.read()
        report = masker.get_report(text)
        print(json.dumps(report, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
