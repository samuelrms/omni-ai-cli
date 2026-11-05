#!/usr/bin/env python3
"""
OpenRouter Client — Chama modelos gratuitos do OpenRouter com PII masking.

Uso:
    export OPENROUTER_API_KEY="sk-or-..."
    python -m omni_ai.openrouter "Qual a diferenca entre REST e GraphQL?"
    python -m omni_ai.openrouter --list-models

Split into submodules: client (the HTTP client). Public names are re-exported here.
"""
import json
import os
import sys
import urllib.request  # kept importable as omni_ai.openrouter.urllib for test patching

from .client import (
    OpenRouterClient, find_best_model,
    OPENROUTER_BASE, PREFERRED_FREE_MODELS, DEFAULT_MODEL, MAX_MODELS,
)

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="OpenRouter free models client com PII masking"
    )
    parser.add_argument("prompt", nargs="*", help="Prompt para o modelo")
    parser.add_argument(
        "--model", "-m", default=DEFAULT_MODEL, help="Modelo a usar"
    )
    parser.add_argument(
        "--no-mask", action="store_true", help="Desabilitar PII masking"
    )
    parser.add_argument(
        "--mask", action="store_true", help="Forçar PII masking (padrão)"
    )
    parser.add_argument(
        "--list-models", "-l", action="store_true", help="Listar modelos gratuitos"
    )
    parser.add_argument(
        "--system", "-s", default=None, help="System prompt"
    )
    parser.add_argument(
        "--temperature", "-t", type=float, default=0.7, help="Temperatura"
    )
    parser.add_argument("--fallback-model", action="append", default=None, help="Modelo gratuito alternativo; pode repetir")
    parser.add_argument("--json", action="store_true", help="Output em JSON")
    parser.add_argument("--allow-paid", action="store_true", help="Autoriza modelos pagos (também via SMART_AI_OPENROUTER_PAID=on)")

    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key and not args.list_models:
        print(
            "❌ OPENROUTER_API_KEY não configurada.\n"
            "   Cadastre-se em https://openrouter.ai e obtenha uma API key.\n"
            "   Depois: export OPENROUTER_API_KEY='sk-or-...'\n"
            "   Ou adicione ao seu ~/.zshrc",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        client = OpenRouterClient(
            api_key=api_key,
            model=args.model,
            enable_masking=not args.no_mask,
            fallback_models=args.fallback_model,
            allow_paid=args.allow_paid or os.environ.get("SMART_AI_OPENROUTER_PAID") == "on",
        )
    except ValueError as error:
        parser.error(str(error))

    if args.list_models:
        try:
            models = client.list_free_models()
        except RuntimeError as e:
            print(f"❌ Erro: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"\n🆓 Modelos gratuitos disponíveis ({len(models)}):\n")
        for m in models:
            ctx = m.get("context_length", "?")
            name = m.get("name", m["id"])
            print(f"  {m['id']:55s}  {name:40s}  ctx={ctx}")
        print()
        return

    prompt = " ".join(args.prompt)
    if not prompt:
        # Ler do stdin
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt:
            parser.print_help()
            sys.exit(1)

    events = os.environ.get('SMART_AI_EVENTS') == '1'
    # Stream tokens for the chat (events) and for a human at a terminal; keep --json buffered.
    if events:
        on_delta = lambda piece: print(json.dumps({'type': 'smart.delta', 'text': piece}, ensure_ascii=False), flush=True)
    elif not args.json:
        on_delta = lambda piece: (sys.stdout.write(piece), sys.stdout.flush())
    else:
        on_delta = None

    try:
        result = client.chat(
            prompt=prompt,
            system_prompt=args.system,
            temperature=args.temperature,
            on_delta=on_delta,
        )

        if events:
            print(json.dumps({'type': 'smart.answer', **result}, ensure_ascii=False), flush=True)
        elif args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif on_delta is None:
            print(result["content"])
        else:
            print()  # end the streamed line

    except RuntimeError as e:
        print(f"❌ Erro: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)

