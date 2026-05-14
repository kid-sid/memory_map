"""Secret redaction utilities."""

import re

# MongoDB URI: mongodb[+srv]://user:PASSWORD@host — redact the password group
_MONGO_RE = re.compile(r'(mongodb(?:\+srv)?://[^:]+:)([^@]+)(@)')

_PATTERNS = [
    # OpenAI project keys
    re.compile(r'sk-proj-[A-Za-z0-9_-]{20,}'),
    # Anthropic keys
    re.compile(r'sk-ant-[A-Za-z0-9_-]{20,}'),
    # OpenAI standard keys (after project/anthropic so short prefixes don't short-circuit)
    re.compile(r'sk-[A-Za-z0-9]{20,}'),
    # AWS access keys
    re.compile(r'AKIA[A-Z0-9]{16}'),
]

# Generic inline assignments: password=..., api_key=..., etc.
_ASSIGN_RE = re.compile(
    r'(?i)(password|secret|token|api_key|apikey)\s*=\s*\S+'
)


def redact_secrets(text: str) -> str:
    """Replace known secret patterns with [REDACTED]. Idempotent."""
    text = _MONGO_RE.sub(r'\1[REDACTED]\3', text)
    for pattern in _PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    text = _ASSIGN_RE.sub(r'\1=[REDACTED]', text)
    return text
