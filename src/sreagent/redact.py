"""Redaction and truncation applied to everything before the model sees it.

Redaction is pattern-based and deliberately errs towards over-redacting: a
log line like `auth=ok` loses its value, which costs nothing, whereas a leaked
token would end up in a third-party API request and possibly a public PR.
"""

import re

REDACTED = "[REDACTED]"

_PATTERNS = [
    # PEM private keys (multi-line)
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL), REDACTED),
    # Anthropic API keys
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"), REDACTED),
    # GitHub tokens: classic/OAuth/app/refresh, and fine-grained PATs
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    # AWS access key IDs (long-term and temporary)
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), REDACTED),
    # JWTs (Kubernetes service account tokens, OIDC tokens)
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), REDACTED),
    # Authorization header values
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + REDACTED),
    # Credentials embedded in URLs: scheme://user:password@host
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1" + REDACTED + "@"),
    # key=value / key: value where the key names a secret
    (re.compile(r"(?i)\b((?:[a-z0-9_-]*)(?:password|passwd|pwd|secret|token|api[_-]?key|"
                r"access[_-]?key|private[_-]?key|credentials?|auth)[\"']?\s*[:=]\s*[\"']?)"
                r"([^\s\"',;&}]{4,})"), r"\1" + REDACTED),
]


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def truncate(text: str, max_chars: int, keep: str = "tail") -> str:
    """Shorten text to about max_chars.

    keep="head": keep the start (newest-first lists, query results).
    keep="tail": keep a little of the start and mostly the end (logs).
    """
    if len(text) <= max_chars:
        return text
    if keep == "head":
        return f"{text[:max_chars]}\n... [truncated {len(text) - max_chars} characters]"
    head = max_chars // 3
    tail = max_chars - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... [truncated {omitted} characters] ...\n{text[-tail:]}"


def sanitize(text: str, max_chars: int, keep: str = "tail") -> str:
    # Redact before truncating so a cut can never leave half a secret behind.
    return truncate(redact(text), max_chars, keep)
