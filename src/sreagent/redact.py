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


# --- Infrastructure identifiers, for anything people read ---------------------------
# Reports, pull request bodies (in a public repository), /investigations and
# the CLI. Not applied to what the model sees: which node or pod IP something
# ran on can matter to a diagnosis.

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_INFRA = [
    # EC2 node names: ip-10-0-11-231.ec2.internal, ip-10-0-1-5.us-east-1.compute.internal
    ("node", re.compile(r"\bip-(?:\d{1,3}-){3}\d{1,3}(?:\.[a-z0-9-]+)*\.(?:ec2|compute)\.internal\b")),
    # IPv4 addresses (a port after them is kept)
    ("ip", re.compile(rf"(?<![\d.]){_OCTET}(?:\.{_OCTET}){{3}}(?![\d.])")),
]
# AWS account IDs where they can be recognised as such: ECR image hosts and ARNs
_ECR_ACCOUNT = re.compile(r"\b\d{12}(?=\.dkr\.ecr\.)")
_ARN_ACCOUNT = re.compile(r"(\barn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:)\d{12}(?=:)")
ACCOUNT = "<account-id>"


def redact_infra(text: str) -> str:
    """Replace node names and IPs with numbered placeholders, and AWS account IDs.

    The same value gets the same placeholder throughout one text, so a reader
    can still tell two nodes apart ("<node-1>" vs "<node-2>").
    """
    text = _ECR_ACCOUNT.sub(ACCOUNT, text)
    text = _ARN_ACCOUNT.sub(lambda m: m.group(1) + ACCOUNT, text)
    for kind, pattern in _INFRA:
        seen: dict[str, str] = {}

        def number(m, kind=kind, seen=seen):
            return seen.setdefault(m.group(0), f"<{kind}-{len(seen) + 1}>")

        text = pattern.sub(number, text)
    return text


def redact_public(text: str) -> str:
    """Secrets and infrastructure identifiers: for text people read."""
    return redact_infra(redact(text))


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
