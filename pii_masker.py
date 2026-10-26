#!/usr/bin/env python3
"""
PII Masker — Mascara dados pessoais/sensíveis antes de enviar para terceiros.

Uso standalone:
    echo "Meu email é foo@bar.com" | python3 pii_masker.py mask
    echo "Meu email é [EMAIL_1]"   | python3 pii_masker.py unmask --map map.json

Uso como módulo:
    from pii_masker import PIIMasker
    masker = PIIMasker()
    masked, mapping = masker.mask("Meu CPF é 123.456.789-00")
    original = masker.unmask(masked, mapping)
"""

import re
import json
import sys
import hashlib
from typing import Tuple


class PIIMasker:
    """Mascara e desmascara dados pessoais identificáveis (PII)."""

    # Padrões organizados por categoria, do mais específico ao mais genérico
    PATTERNS = [
        # === Credenciais e Secrets (ALTA PRIORIDADE) ===
        (
            "API_KEY",
            re.compile(
                r"""(?:sk-(?:ant-)?[a-zA-Z0-9_-]{20,}"""       # Anthropic/OpenAI keys
                r"""|ghp_[a-zA-Z0-9]{36}"""                      # GitHub PAT
                r"""|gho_[a-zA-Z0-9]{36}"""                      # GitHub OAuth
                r"""|github_pat_[a-zA-Z0-9_]{22,}"""             # GitHub fine-grained
                r"""|glpat-[a-zA-Z0-9_-]{20,}"""                 # GitLab PAT
                r"""|xoxb-[0-9A-Za-z-]{50,}"""                   # Slack bot token
                r"""|xoxp-[0-9A-Za-z-]{50,}"""                   # Slack user token
                r"""|AKIA[0-9A-Z]{16}"""                         # AWS Access Key
                r"""|AIza[0-9A-Za-z_-]{35}"""                    # Google API Key
                r"""|ya29\.[0-9A-Za-z_-]+"""                     # Google OAuth
                r"""|eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})""",  # JWT
                re.VERBOSE,
            ),
        ),
        (
            "SECRET",
            re.compile(
                r"(?:password|passwd|pwd|secret|token|api[_-]?key|auth)\s*[:=]\s*['\"]?([^\s'\"]{8,})['\"]?",
                re.IGNORECASE,
            ),
        ),

        # === Documentos Brasileiros ===
        ("CPF", re.compile(r"\b\d{3}[.\s]?\d{3}[.\s]?\d{3}[-.\s]?\d{2}\b")),
        ("CNPJ", re.compile(r"\b\d{2}[.\s]?\d{3}[.\s]?\d{3}[/\\]?\d{4}[-.\s]?\d{2}\b")),
        ("RG", re.compile(r"\b\d{2}[.\s]?\d{3}[.\s]?\d{3}[-.\s]?[\dxX]\b")),

        # === Documentos Internacionais ===
        ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),  # US Social Security

        # === Cartões de Crédito ===
        (
            "CREDIT_CARD",
            re.compile(
                r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))"
                r"[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{3,4}\b"
            ),
        ),

        # === Telefones ===
        (
            "PHONE_BR",
            re.compile(
                r"(?:\+55\s?)?(?:\(?\d{2}\)?\s?)?\d{4,5}[-.\s]?\d{4}\b"
            ),
        ),
        (
            "PHONE_INTL",
            re.compile(r"\+\d{1,3}[\s.-]?\(?\d{1,4}\)?[\s.-]?\d{3,4}[\s.-]?\d{3,4}\b"),
        ),

        # === Endereços IP ===
        (
            "IPV4",
            re.compile(
                r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
                r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
            ),
        ),
        (
            "IPV6",
            re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"),
        ),

        # === Email (depois de IP para não conflitar) ===
        (
            "EMAIL",
            re.compile(
                r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"
            ),
        ),

        # === URLs com credenciais embutidas ===
        (
            "URL_WITH_CREDS",
            re.compile(
                r"https?://[^:]+:[^@]+@[^\s]+"
            ),
        ),

        # === Caminhos de arquivo com usernames ===
        (
            "FILE_PATH",
            re.compile(
                r"(?:/(?:Users|home)/[a-zA-Z0-9._-]+)"
            ),
        ),

        # === Nomes próprios em contexto sensível ===
        # Não fazemos detecção de nomes genéricos (muito falso positivo),
        # mas pegamos padrões como "nome: Fulano" ou "name: John"
        (
            "PERSON_NAME",
            re.compile(
                r"(?:nome|name|author|autor|user(?:name)?|responsável)\s*[:=]\s*['\"]?([A-ZÀ-Ú][a-zà-ú]+(?:\s+[A-ZÀ-Ú][a-zà-ú]+){1,4})['\"]?",
                re.IGNORECASE,
            ),
        ),

        # === Endereços físicos (CEP brasileiro) ===
        ("CEP", re.compile(r"\b\d{5}[-.]?\d{3}\b")),

        # === Chaves privadas ===
        (
            "PRIVATE_KEY",
            re.compile(
                r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
                r"[\s\S]*?"
                r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"
            ),
        ),

        # === Connection strings ===
        (
            "CONN_STRING",
            re.compile(
                r"(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://"
                r"[^\s]+"
            ),
        ),
    ]

    # Padrões que são comuns em código e NÃO devem ser mascarados
    FALSE_POSITIVE_PATTERNS = [
        re.compile(r"\b(?:127\.0\.0\.1|0\.0\.0\.0|localhost)\b"),  # IPs locais
        re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{1,2}\b"),  # Versões (1.2.3)
    ]

    def __init__(self, extra_patterns: list = None):
        """
        Args:
            extra_patterns: Lista de tuplas (nome, regex_compilado) adicionais
        """
        self.patterns = self.PATTERNS.copy()
        if extra_patterns:
            self.patterns.extend(extra_patterns)

    def _is_false_positive(self, match_text: str, category: str) -> bool:
        """Verifica se um match é falso positivo."""
        # IPs locais/comuns
        if category in ("IPV4",):
            for fp in self.FALSE_POSITIVE_PATTERNS:
                if fp.match(match_text):
                    return True
        # CEPs que parecem mais versões ou números genéricos
        if category == "CEP" and len(match_text.replace("-", "").replace(".", "")) < 8:
            return True
        return False

    def mask(self, text: str, namespace: str = "") -> Tuple[str, dict]:
        """Replace non-overlapping spans; use distinct namespaces across messages."""
        import uuid
        namespace = namespace or uuid.uuid4().hex[:12]
        mapping, replacements, counters = {}, [], {}
        # Cover complete credentials before patterns nested inside them.
        first = {'PRIVATE_KEY', 'CONN_STRING', 'URL_WITH_CREDS'}
        patterns = sorted(self.patterns, key=lambda item: item[0] not in first)
        for category, pattern in patterns:
            for match in pattern.finditer(text):
                group = match.lastindex or 0
                start, end = match.span(group)
                original = match.group(group)
                if self._is_false_positive(original, category):
                    continue
                if any(start < right and left < end for left, right, _ in replacements):
                    continue
                counters[category] = counters.get(category, 0) + 1
                placeholder = f"[{category}_{namespace}_{counters[category]}]"
                while placeholder in text or placeholder in mapping:
                    counters[category] += 1
                    placeholder = f"[{category}_{namespace}_{counters[category]}]"
                mapping[placeholder] = original
                replacements.append((start, end, placeholder))
        result = text
        for start, end, placeholder in sorted(replacements, reverse=True):
            result = result[:start] + placeholder + result[end:]
        return result, mapping

    def unmask(self, text: str, mapping: dict) -> str:
        """Restore only known placeholders, in one pass."""
        if not mapping:
            return text
        return re.sub('|'.join(re.escape(key) for key in mapping), lambda match: mapping[match.group()], text)

    def mask_for_log(self, text: str) -> str:
        """Mascara PII para logging (sem mapa de reversão, irreversível)."""
        masked, _ = self.mask(text)
        return masked

    def get_report(self, text: str) -> dict:
        """Retorna relatório de PII encontrado no texto."""
        _, mapping = self.mask(text)
        report = {"total_found": len(mapping), "categories": {}}
        for placeholder in mapping:
            category = placeholder.split("_")[0].strip("[")
            report["categories"][category] = report["categories"].get(category, 0) + 1
        return report


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

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
            with open(args.map_out, "w") as f:
                json.dump(mapping, f, indent=2, ensure_ascii=False)
            print(f"\n[PII Map saved to {args.map_out}]", file=sys.stderr)
        if args.report:
            report = masker.get_report(text)
            print(f"\n[PII Report: {json.dumps(report)}]", file=sys.stderr)

    elif args.command == "unmask":
        text = sys.stdin.read()
        with open(args.map, "r") as f:
            mapping = json.load(f)
        print(masker.unmask(text, mapping), end="")

    elif args.command == "report":
        text = sys.stdin.read()
        report = masker.get_report(text)
        print(json.dumps(report, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
