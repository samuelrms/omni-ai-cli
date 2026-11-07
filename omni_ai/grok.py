#!/usr/bin/env python3
"""
Grok Client — Chama a API da xAI (paga por uso) com PII masking.

Uso:
    export XAI_API_KEY="xai-..."
    echo "Olá mundo" | python -m omni_ai.grok
    python -m omni_ai.grok --model grok-4-fast-non-reasoning "Olá mundo"
"""

import http.client
import json
import os
import sys
import urllib.error
import urllib.request

from .pii import PIIMasker

XAI_BASE = "https://api.x.ai/v1"


def request(endpoint, key, data=None, timeout=60, base=XAI_BASE, name="xAI"):
    """HTTP call to an OpenAI-compatible API; errors carry only the provider's short message, never the prompt."""
    req = urllib.request.Request(f"{base}{endpoint}", data=json.dumps(data).encode() if data else None,
                                 method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                                          "User-Agent": "omni-ai-cli"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            reason = str(body.get("error", {}).get("message") if isinstance(body.get("error"), dict) else body.get("error", ""))[:160]
        except (ValueError, AttributeError, OSError):
            reason = ""
        e.close()
        raise RuntimeError(f"{name} HTTP {e.code}" + (f": {reason}" if reason else "")) from None
    except urllib.error.URLError:
        raise RuntimeError(f"Falha de rede ao acessar a {name}") from None
    except (ValueError, TimeoutError, http.client.HTTPException, OSError):
        # includes a connection reset or IncompleteRead mid-body
        raise RuntimeError(f"Resposta inválida ou timeout da {name}") from None


def pick_model(key):
    """No model configured: the fastest non-reasoning Grok the account can use."""
    data = request("/models", key).get("data", [])
    ids = sorted((m.get("id", "") for m in data if isinstance(m, dict) and m.get("id")), reverse=True)
    for wanted in (lambda i: "fast" in i and "non-reasoning" in i, lambda i: "fast" in i, lambda i: True):
        found = [i for i in ids if i.startswith("grok") and wanted(i)]
        if found:
            return found[0]
    raise RuntimeError("Nenhum modelo Grok disponível nesta conta")


def chat(key, prompt, model="", system_prompt=None, mask=True, base=XAI_BASE, name="xAI"):
    masker = PIIMasker() if mask else None
    pii_map = {}
    if masker:
        prompt, pii_map = masker.mask(prompt)
        if system_prompt:
            system_prompt, more = masker.mask(system_prompt)
            pii_map.update(more)
        if pii_map:
            print(f"  🛡️  PII mascarado: {len(pii_map)} item(s)", file=sys.stderr)
    messages = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [{"role": "user", "content": prompt}]
    data = {"model": model or pick_model(key), "messages": messages}
    # Set by the server for one request
    if os.environ.get("SMART_AI_TEMPERATURE"):
        data["temperature"] = float(os.environ["SMART_AI_TEMPERATURE"])
    if os.environ.get("SMART_AI_MAX_TOKENS"):
        data["max_tokens"] = int(os.environ["SMART_AI_MAX_TOKENS"])
    if os.environ.get("SMART_AI_EVENTS") == "1":
        print(json.dumps({"type": "smart.request", "data": data}, ensure_ascii=False), file=sys.stderr, flush=True)
    resp = request("/chat/completions", key, data, base=base, name=name)
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"{name} não retornou uma resposta válida") from None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"{name} retornou conteúdo vazio")
    if masker and pii_map:
        content = masker.unmask(content, pii_map)
    return {"content": content, "model": resp.get("model", data["model"]), "masked_pii_count": len(pii_map),
            "usage": resp.get("usage", {})}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Grok (xAI) client com PII masking")
    parser.add_argument("prompt", nargs="*", help="Prompt para o modelo")
    parser.add_argument("--model", "-m", default="", help="Modelo a usar (vazio = mais rápido disponível)")
    parser.add_argument("--system", "-s", default=None, help="System prompt")
    parser.add_argument("--no-mask", action="store_true", help="Desabilitar PII masking")
    args = parser.parse_args()

    key = os.environ.get("XAI_API_KEY", "")
    if not key:
        print("❌ XAI_API_KEY não configurada. Crie em https://console.x.ai e rode: ai --key grok", file=sys.stderr)
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
    from .osutils import utf8_stdio
    utf8_stdio()
    main()
