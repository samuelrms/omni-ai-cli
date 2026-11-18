"""PIIMasker: detect and reversibly mask personal data before sending prompts to third-party LLMs."""
import ipaddress
import re
from typing import Tuple

NOT_A_VALUE = r"(?!(?:string|str|null|none|true|false|undefined|any|int|bool|number)\b)"


def _secret_pattern(words, shortest):
    """A word, its optional ending (aws_secret_access_key), and a value that may be quoted, as in JSON."""
    return re.compile(
        words + r"(?:[_-][\w-]{0,40})?[\"']?\s*[:=]\s*" + NOT_A_VALUE +
        rf"(?:\"([^\"\n]{{{shortest},}})\"|'([^'\n]{{{shortest},}})'|([^\s'\",;{{}}()]{{{shortest},}}))",
        re.IGNORECASE,
    )


def _check_digit(total):
    """The Receita's rule for CPF and CNPJ: a remainder of 0 or 1 modulo 11 gives 0, any other gives 11 minus it."""
    rest = total % 11
    return 0 if rest < 2 else 11 - rest


def valid_cpf(digits: str) -> bool:
    """Eleven digits whose last two are the check digits of the first nine, as in the Receita's TestaCPF.
    All zeros is not valid; other repeated digits are, as there."""
    if not re.fullmatch(r"[0-9]{11}", digits) or digits == "0" * 11:
        return False
    numbers = [int(char) for char in digits]
    first = _check_digit(sum(n * w for n, w in zip(numbers[:9], range(10, 1, -1))))
    second = _check_digit(sum(n * w for n, w in zip(numbers[:10], range(11, 1, -1))))
    return numbers[9:] == [first, second]


CNPJ_WEIGHTS = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)


def valid_cnpj(chars: str) -> bool:
    """Twelve characters in 0-9 or A-Z and two check digits, by the Receita's rule for the numeric and the
    alphanumeric CNPJ: each character is worth its ASCII code minus 48. All zeros is not valid."""
    if not re.fullmatch(r"[0-9A-Z]{12}[0-9]{2}", chars) or chars == "0" * 14:
        return False
    values = [ord(char) - 48 for char in chars]
    first = _check_digit(sum(v * w for v, w in zip(values[:12], CNPJ_WEIGHTS[1:])))
    second = _check_digit(sum(v * w for v, w in zip(values[:13], CNPJ_WEIGHTS)))
    return values[12:] == [first, second]


class PIIMasker:
    """Mascara e desmascara dados pessoais identificáveis (PII)."""

    # Padrões organizados por categoria, do mais específico ao mais genérico
    PATTERNS = [
        # === Credenciais e Secrets (ALTA PRIORIDADE) ===
        (
            "API_KEY",
            re.compile(
                r"""(?:sk-(?:ant-)?[a-zA-Z0-9_-]{20,}"""       # Anthropic/OpenAI keys
                r"""|csk-[a-zA-Z0-9_-]{20,}"""                   # Cerebras
                r"""|xai-[a-zA-Z0-9_-]{20,}"""                   # xAI
                r"""|gsk_[a-zA-Z0-9_-]{20,}"""                   # Groq
                r"""|npm_[a-zA-Z0-9]{20,}"""                     # npm
                r"""|gh[pousr]_[a-zA-Z0-9]{20,}"""               # GitHub tokens
                r"""|github_pat_[a-zA-Z0-9_]{22,}"""             # GitHub fine-grained
                r"""|glpat-[a-zA-Z0-9_-]{20,}"""                 # GitLab PAT
                r"""|xoxb-[0-9A-Za-z-]{50,}"""                   # Slack bot token
                r"""|xoxp-[0-9A-Za-z-]{50,}"""                   # Slack user token
                r"""|(?:AKIA|ASIA)[0-9A-Z]{16}"""                # AWS access key, long-lived or temporary
                r"""|AIza[0-9A-Za-z_-]{30,}"""                   # Google API Key
                r"""|ya29\.[0-9A-Za-z_-]+"""                     # Google OAuth
                r"""|eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})""",  # JWT
                re.VERBOSE,
            ),
        ),
        # Passwords can be short. The other words appear in ordinary text, so only long values count.
        ("SECRET", _secret_pattern(r"(?:password|passwd|pwd|senha)", 1)),
        ("SECRET", _secret_pattern(r"(?:secret|token|api[_-]?key|access[_-]?key|auth(?:orization)?|chave)", 8)),
        ("SECRET", re.compile(r"\bbearer\s+((?=[A-Za-z]{0,300}[^A-Za-z\s])[A-Za-z0-9._~+/=-]{8,})", re.IGNORECASE)),
        ("SECRET", re.compile(r"authorization[\"']?\s*[:=]\s*[\"']?basic\s+([A-Za-z0-9+/=]{4,})", re.IGNORECASE)),

        # === Documentos Brasileiros ===
        ("CPF", re.compile(r"\b\d{3}[.\s]?\d{3}[.\s]?\d{3}[-.\s]?\d{2}\b")),
        ("CNPJ", re.compile(r"\b\d{2}[.\s]?\d{3}[.\s]?\d{3}[/\\]?\d{4}[-.\s]?\d{2}\b")),
        # The alphanumeric CNPJ, issued from July 2026. Lowercase is taken too, except with dots, where
        # it looks more like a host and a path (ab.com.net/blog-44) than a CNPJ
        (
            "CNPJ",
            re.compile(
                r"\b(?:[0-9A-Z]{2}\.[0-9A-Z]{3}\.[0-9A-Z]{3}/[0-9A-Z]{4}-"
                r"|[0-9A-Za-z]{8}/[0-9A-Za-z]{4}-"
                r"|[0-9A-Za-z]{12})\d{2}\b(?!@)"  # before an @ it is the name in an email, which EMAIL takes whole
            ),
        ),
        ("RG", re.compile(r"\b\d{2}[.\s]?\d{3}[.\s]?\d{3}[-.\s]?[\dxX]\b")),
        ("PIS", re.compile(r"\b\d{3}\.\d{5}\.\d{2}-\d\b")),

        # === Documentos Internacionais ===
        ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),  # US Social Security

        # === Cartões de Crédito ===
        (
            "CREDIT_CARD",
            re.compile(
                r"\b(?:3[47]\d{2}[\s-]?\d{6}[\s-]?\d{5}"  # American Express groups its digits differently
                r"|(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))"
                r"[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{3,4})\b"
            ),
        ),

        # === Telefones ===
        # International and US first: the Brazilian pattern would take only the end of these numbers
        (
            "PHONE_INTL",
            re.compile(r"\+(?!55)\d{1,3}(?:[\s.-]?\(?\d{1,4}\)?){2,5}\b"),
        ),
        ("PHONE_US", re.compile(r"(?:\(\d{3}\)\s?|\b\d{3}-)\d{3}[-.\s]\d{4}\b")),
        (
            "PHONE_BR",
            re.compile(
                r"(?:\+55\s?)?(?:\(?\d{2}\)?\s?)?\d{4,5}[-.\s]?\d{4}\b"
            ),
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
            re.compile(r"(?<![\w:.])[0-9a-fA-F:]{2,39}(?![\w:.])"),  # the shape only: _is_false_positive validates it
        ),

        # === Email (depois de IP para não conflitar) ===
        (
            "EMAIL",
            re.compile(
                r"\b[\w.%+-]{1,64}@[\w.-]{1,255}\.[^\W\d_]{2,}\b"
            ),
        ),

        # === URLs com credenciais embutidas ===
        (
            "URL_WITH_CREDS",
            re.compile(
                r"https?://[^\s:/@]{1,256}:[^\s/@]{1,256}@\S+"
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
                r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
                r"[\s\S]*?"
                r"(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----|\Z)"  # cut off, it is still a secret
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
                # fullmatch so the 3-octet version pattern can't match a real IPv4 prefix
                # (e.g. "10.0.0.1"), which used to leave such IPs unmasked.
                if fp.fullmatch(match_text):
                    return True
        if category == "IPV6":
            try:
                address = ipaddress.IPv6Address(match_text)
            except ValueError:
                return True
            # `ab::cd` is valid but is far more likely a name in code than an address
            return address.is_loopback or not any(char.isdigit() for char in match_text)
        # The shape of a CPF or CNPJ is common in ordinary numbers and codes: only valid check digits make it one.
        # \d also matches other scripts' digits (１２３), which the check needs as 0-9
        if category in ("CPF", "CNPJ"):
            ascii_digits = re.sub(r"\d", lambda digit: str(int(digit.group())), match_text)
            if category == "CPF":
                return not valid_cpf(re.sub(r"[.\s-]", "", ascii_digits))
            return not valid_cnpj(re.sub(r"[.\s/\\-]", "", ascii_digits).upper())
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

    @staticmethod
    def unmask(text: str, mapping: dict) -> str:
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
            # Placeholder is [CATEGORY_namespace_counter]; CATEGORY itself may contain '_'
            # (e.g. URL_WITH_CREDS), so strip only the trailing _namespace_counter.
            found = re.match(r"\[(.+)_[^_]+_\d+\]$", placeholder)
            category = found.group(1) if found else placeholder.split("_")[0].strip("[")
            report["categories"][category] = report["categories"].get(category, 0) + 1
        return report


class StreamUnmasker:
    """Restores a streamed answer piece by piece, where a placeholder may arrive split across pieces."""

    def __init__(self, mapping: dict):
        self.mapping, self.pending = mapping, ""

    def feed(self, piece: str) -> str:
        """What can be shown now, restored. An ending that may still become a placeholder waits for more text."""
        text, self.pending = self.pending + piece, ""
        # A placeholder made by mask() has one "[", at its start, so only the text from the last "[" can be one still arriving
        start = text.rfind("[")
        ending = text[start:] if start >= 0 else ""
        if ending and any(key != ending and key.startswith(ending) for key in self.mapping):
            text, self.pending = text[:start], ending
        return PIIMasker.unmask(text, self.mapping)

    def flush(self) -> str:
        """The ending still held when the answer ends. It never became a placeholder, so it goes out as it is."""
        text, self.pending = self.pending, ""
        return text


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

