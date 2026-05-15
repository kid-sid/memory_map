"""Secret redaction utilities."""

import logging
import os
import re

logger = logging.getLogger(__name__)

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
    # GitHub classic PAT
    re.compile(r'ghp_[A-Za-z0-9]{36}'),
    # GitHub fine-grained PAT
    re.compile(r'github_pat_[A-Za-z0-9_]{82}'),
]

# Generic inline assignments: password=..., api_key=..., etc.
_ASSIGN_RE = re.compile(
    r'(?i)(password|secret|token|api_key|apikey)\s*=\s*\S+'
)


def _load_user_patterns() -> list:
    """Compile patterns from MCP_REDACT_PATTERNS (|-delimited). Invalid entries are skipped."""
    raw = os.environ.get("MCP_REDACT_PATTERNS", "")
    compiled = []
    for part in raw.split("|"):
        part = part.strip()
        if not part:
            continue
        try:
            compiled.append(re.compile(part))
        except re.error as exc:
            logger.warning("memory_map: invalid MCP_REDACT_PATTERNS entry %r — skipped (%s)", part, exc)
    return compiled


_USER_PATTERNS: list = _load_user_patterns()


def redact_secrets(text: str) -> str:
    """Replace known secret patterns with [REDACTED]. Idempotent."""
    text = _MONGO_RE.sub(r'\1[REDACTED]\3', text)
    for pattern in _PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    text = _ASSIGN_RE.sub(r'\1=[REDACTED]', text)
    for pattern in _USER_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text
