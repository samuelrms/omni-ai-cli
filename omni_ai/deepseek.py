#!/usr/bin/env python3
"""
DeepSeek Client — Chama a API da DeepSeek (paga por uso, barata) com PII masking.

Uso:
    export DEEPSEEK_API_KEY="sk-..."
    echo "Olá mundo" | python -m omni_ai.deepseek
    python -m omni_ai.deepseek --model deepseek-reasoner "Olá mundo"
"""

import json
import os
import sys

from . import grok

DEEPSEEK_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def chat(key, prompt, model="", system_prompt=None, mask=True):
    return grok.chat(key, prompt, model or DEFAULT_MODEL, system_prompt, mask, base=DEEPSEEK_BASE, name="DeepSeek")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="DeepSeek client com PII masking")
    parser.add_argument("prompt", nargs="*", help="Prompt para o modelo")
    parser.add_argument("--model", "-m", default="", help=f"Modelo a usar (vazio = {DEFAULT_MODEL})")
    parser.add_argument("--system", "-s", default=None, help="System prompt")
    parser.add_argument("--no-mask", action="store_true", help="Desabilitar PII masking")
    args = parser.parse_args()

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("❌ DEEPSEEK_API_KEY não configurada. Crie em https://platform.deepseek.com/api_keys e rode: ai --key deepseek", file=sys.stderr)
        sys.exit(1)
    prompt = " ".join(args.prompt) or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    if not prompt:
        parser.print_help()
        sys.exit(1)
    try:
        result = chat(key, prompt, args.model, args.system, mask=not args.no_mask)
    except RuntimeError as e:
        print(f"❌ Erro: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    if os.environ.get("SMART_AI_EVENTS") == "1":
        print(json.dumps({"type": "smart.answer", **result}, ensure_ascii=False), flush=True)
    else:
        print(result["content"])


if __name__ == "__main__":
    main()
