#!/usr/bin/env python3
"""
OpenRouter Client — Chama modelos gratuitos do OpenRouter com PII masking.

Uso:
    export OPENROUTER_API_KEY="sk-or-..."
    python -m omni_ai.openrouter "Qual a diferença entre REST e GraphQL?"
    
    # Com PII masking (padrão)
    python -m omni_ai.openrouter --mask "Meu email é foo@bar.com, explique OAuth"
    
    # Sem masking
    python -m omni_ai.openrouter --no-mask "Explique o que é Docker"
    
    # Modelo específico
    python -m omni_ai.openrouter --model "google/gemma-4-31b-it:free" "Olá mundo"
    
    # Listar modelos gratuitos
    python -m omni_ai.openrouter --list-models
"""

import json
import re
import os
import sys
import http.client
import urllib.request
import urllib.error
from typing import Optional

# Importar o PII masker do mesmo diretório
from .pii import PIIMasker


# ─────────────────────────────────────────────
# Configuração
# ─────────────────────────────────────────────

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Modelos gratuitos recomendados, em ordem de preferência
PREFERRED_FREE_MODELS = [
    "google/gemma-4-31b-it:free",               # Gemma 4 31B — forte geral
    "nvidia/nemotron-3.5-lightning:free",         # Nemotron Lightning — rápido
    "nvidia/nemotron-3-ultra-550b-a55b:free",     # Nemotron Ultra — mais capaz
    "google/gemma-4-26b-a4b-it:free",             # Gemma 4 26B
    "thinkingmachines/inkling:free",              # Inkling — 1M context
    "cohere/north-mini-code:free",                # North Mini — foco em código
    "openrouter/free",                            # Router automático (fallback)
]

DEFAULT_MODEL = "openrouter/free"
MAX_MODELS = 3


class OpenRouterClient:
    """Cliente para a API do OpenRouter com PII masking integrado."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        enable_masking: bool = True,
        fallback_models: Optional[list] = None,
        allow_paid: bool = False,
    ):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.model = model
        self.fallback_models = fallback_models
        self.allow_paid = allow_paid
        for candidate in [model] + (fallback_models or []):
            if not allow_paid and candidate != DEFAULT_MODEL and not candidate.endswith(':free'):
                raise ValueError('Modelo pago não autorizado. Libere com: /model openrouter_paid on')
        self.masker = PIIMasker() if enable_masking else None
        self._pii_map = {}

    def _request(self, endpoint: str, data: dict = None, method: str = "GET") -> dict:
        """Faz uma request HTTP para o OpenRouter."""
        if os.environ.get('SMART_AI_EVENTS') == '1' and endpoint == '/chat/completions':
            print(json.dumps({'type': 'smart.request', 'data': data}, ensure_ascii=False), file=sys.stderr, flush=True)
        url = f"{OPENROUTER_BASE}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/samuelrms/omni-ai-cli",
            "X-Title": "Omni AI CLI",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            # Only the provider's short error message is shown; the rest of the body could echo the prompt
            try:
                reason = json.loads(e.read().decode())['error']['message'][:160]
            except (ValueError, KeyError, TypeError, OSError):
                reason = ''
            e.close()
            raise RuntimeError(f"OpenRouter HTTP {e.code}" + (f": {reason}" if reason else '')) from None
        except urllib.error.URLError as e:
            raise RuntimeError("Falha de rede ao acessar OpenRouter") from None
        except (ValueError, TimeoutError, http.client.HTTPException, OSError):
            raise RuntimeError("Resposta inválida ou timeout do OpenRouter") from None

    def _stream_request(self, data: dict):
        """POST /chat/completions with stream=True, yielding ('delta', text) per token and a final
        ('final', {content, model, usage}). Raises RuntimeError on any failure, like _request."""
        if os.environ.get('SMART_AI_EVENTS') == '1':
            print(json.dumps({'type': 'smart.request', 'data': data}, ensure_ascii=False), file=sys.stderr, flush=True)
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/samuelrms/omni-ai-cli",
            "X-Title": "Omni AI CLI",
            "Accept": "text/event-stream",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = json.dumps(dict(data, stream=True, stream_options={"include_usage": True})).encode()
        req = urllib.request.Request(f"{OPENROUTER_BASE}/chat/completions", data=body, headers=headers, method="POST")
        content, model, usage = [], None, {}
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        reason = (chunk["error"].get("message") if isinstance(chunk["error"], dict) else str(chunk["error"]))
                        raise RuntimeError("OpenRouter: " + (reason or "erro no stream"))
                    model = chunk.get("model") or model
                    usage = chunk.get("usage") or usage
                    for choice in (chunk.get("choices") or []):
                        if not isinstance(choice, dict):
                            continue
                        piece = (choice.get("delta") or {}).get("content") or ""
                        if piece:
                            content.append(piece)
                            yield ("delta", piece)
        except urllib.error.HTTPError as e:
            try:
                reason = json.loads(e.read().decode())["error"]["message"][:160]
            except (ValueError, KeyError, TypeError, OSError):
                reason = ""
            e.close()
            raise RuntimeError(f"OpenRouter HTTP {e.code}" + (f": {reason}" if reason else "")) from None
        except urllib.error.URLError:
            raise RuntimeError("Falha de rede ao acessar OpenRouter") from None
        except (ValueError, TimeoutError, http.client.HTTPException, OSError):
            raise RuntimeError("Resposta inválida ou timeout do OpenRouter") from None
        yield ("final", {"content": "".join(content), "model": model, "usage": usage})

    def list_free_models(self) -> list:
        """Lista todos os modelos gratuitos disponíveis."""
        resp = self._request("/models")
        free = [
            m
            for m in resp.get("data", [])
            if m.get("pricing", {}).get("prompt") == "0"
            and m.get("pricing", {}).get("completion") == "0"
        ]
        return sorted(free, key=lambda x: x["id"])

    def chat(
        self,
        prompt: str,
        system_prompt: str = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        on_delta=None,
    ) -> dict:
        """
        Envia um prompt e retorna a resposta.

        Se masking estiver habilitado:
        1. Mascara PII no prompt
        2. Envia o prompt mascarado
        3. Desmascara PII na resposta

        Returns:
            dict com 'content', 'model', 'masked_pii_count', 'usage'
        """
        pii_report = None
        self._pii_map = {}

        # === PII Masking ===
        if self.masker:
            masked_prompt, self._pii_map = self.masker.mask(prompt)
            pii_report = self.masker.get_report(prompt)

            if pii_report["total_found"] > 0:
                prompt = masked_prompt
                # Log para stderr (não interfere com output)
                categories = ", ".join(
                    f"{k}:{v}" for k, v in pii_report["categories"].items()
                )
                print(
                    f"  🛡️  PII mascarado: {pii_report['total_found']} item(s) [{categories}]",
                    file=sys.stderr,
                )

            # Também mascarar o system prompt se houver
            if system_prompt and self.masker:
                system_prompt, sys_map = self.masker.mask(system_prompt, namespace="SYSTEM")
                self._pii_map.update(sys_map)

        # === Montar mensagens ===
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # === Chamar API ===
        models = self.fallback_models
        if models is None:
            try:
                available = [m['id'] for m in self.list_free_models() if m['id'].endswith(':free')]
                models = [m for m in PREFERRED_FREE_MODELS if m in available]
                models += [m for m in available if m not in models]
            except RuntimeError:
                models = []
        # Every free model gets a turn. OpenRouter accepts at most 3 per request and falls back inside
        # each group itself; when a whole group fails we move on to the next, ending with its free router.
        candidates = list(dict.fromkeys(([self.model] if self.model != DEFAULT_MODEL else []) + models + [DEFAULT_MODEL]))
        groups = [candidates[i:i + MAX_MODELS] for i in range(0, len(candidates), MAX_MODELS)]
        resp, last_error = None, None
        streamed_any = False
        for group in groups:
            data = {"models": group, "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
                    "usage": {"include": True}}  # the reply then carries what it cost
            # Paid models only when explicitly authorized; otherwise the price cap blocks them
            if not self.allow_paid:
                data["provider"] = {"max_price": {"prompt": 0, "completion": 0}}
            try:
                if on_delta is not None:
                    # Stream the tokens as they arrive; a chunk may still be a masked placeholder, so
                    # unmask each piece best-effort and unmask the whole answer again at the end.
                    final = None
                    gen = self._stream_request(data)
                    try:
                        for kind, payload in gen:
                            if kind == "delta":
                                streamed_any = True
                                piece = self.masker.unmask(payload, self._pii_map) if (self.masker and self._pii_map) else payload
                                on_delta(piece)
                            else:
                                final = payload
                    finally:
                        gen.close()  # close the HTTP response if the consumer stops early (e.g. BrokenPipe)
                    if final and final["content"].strip():
                        resp = {"choices": [{"message": {"content": final["content"]}}],
                                "model": final["model"], "usage": final["usage"]}
                        break
                    last_error = RuntimeError("OpenRouter não retornou uma resposta válida")
                else:
                    resp = self._request("/chat/completions", data=data, method="POST")
                    if resp.get("choices"):
                        break
                    last_error = RuntimeError("OpenRouter não retornou uma resposta válida")
            except RuntimeError as error:
                last_error = error
                # Already streamed part of an answer before failing: trying another model would
                # splice two different answers in the output, so stop here instead of retrying.
                if streamed_any:
                    raise
                # Bad key or no credit: another model won't help
                if re.search(r"HTTP (401|402|403)\b", str(error)):
                    raise
            if os.environ.get('SMART_AI_EVENTS') != '1':
                print(f"  ↪ {', '.join(group)} indisponíveis; tentando os próximos modelos gratuitos...", file=sys.stderr)
        if resp is None or not resp.get("choices"):
            raise last_error or RuntimeError("OpenRouter não retornou uma resposta válida")

        # === Extrair resposta ===
        choices = resp.get("choices", [])
        if not choices:
            raise RuntimeError("OpenRouter não retornou uma resposta válida")

        content = choices[0].get("message", {}).get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenRouter retornou conteúdo vazio ou inválido")
        model_used = resp.get("model", self.model)
        usage = resp.get("usage", {})

        # === Desmascarar resposta ===
        if self.masker and self._pii_map:
            content = self.masker.unmask(content, self._pii_map)

        return {
            "content": content,
            "model": model_used,
            "masked_pii_count": len(self._pii_map),
            "usage": usage,
        }


def find_best_model(client: OpenRouterClient) -> str:
    """Tenta encontrar o melhor modelo free disponível."""
    try:
        available = {m["id"] for m in client.list_free_models()}
        for model in PREFERRED_FREE_MODELS:
            if model in available:
                return model
    except Exception:
        pass
    return DEFAULT_MODEL


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

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


if __name__ == "__main__":
    main()
