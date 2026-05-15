import os
import pytest
from memory_map_mcp.redact import redact_secrets, _load_user_patterns


# --- Pattern coverage ---

def test_openai_project_key():
    text = "key is sk-proj-AbCdEfGhIjKlMnOpQrStUvWx123456"
    result = redact_secrets(text)
    assert "[REDACTED]" in result
    assert "AbCdEfGhIjKlMnOpQrStUvWx123456" not in result


def test_openai_standard_key():
    text = "export OPENAI_KEY=sk-AbCdEfGhIjKlMnOpQrStUv1234567890xx"
    result = redact_secrets(text)
    assert "AbCdEfGhIjKlMnOpQrStUv" not in result
    assert "[REDACTED]" in result


def test_anthropic_key():
    text = "using sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz1234"
    result = redact_secrets(text)
    assert "[REDACTED]" in result
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz1234" not in result


def test_github_classic_pat():
    text = "token: ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890"
    result = redact_secrets(text)
    assert "[REDACTED]" in result
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz1234567890" not in result


def test_github_fine_grained_pat():
    pat = "github_pat_" + "A" * 82
    result = redact_secrets(f"GITHUB_TOKEN={pat}")
    assert "[REDACTED]" in result
    assert "A" * 82 not in result


def test_aws_access_key():
    text = "aws key: AKIAIOSFODNN7EXAMPLE"
    result = redact_secrets(text)
    assert "[REDACTED]" in result
    assert "AKIAIOSFODNN7EXAMPLE" not in result


def test_mongodb_uri_password():
    text = "mongodb://alice:s3cr3tpassword@cluster.mongodb.net/db"
    result = redact_secrets(text)
    assert "s3cr3tpassword" not in result
    assert "[REDACTED]" in result
    # URI prefix and host preserved
    assert "mongodb://" in result
    assert "@cluster.mongodb.net" in result


def test_mongodb_srv_uri():
    text = "mongodb+srv://user:hunter2@host.example.com/db"
    result = redact_secrets(text)
    assert "hunter2" not in result
    assert "mongodb+srv://" in result


def test_generic_password():
    text = "password=hunter2"
    result = redact_secrets(text)
    assert "hunter2" not in result
    assert "password=[REDACTED]" in result


def test_generic_secret_case_insensitive():
    text = "SECRET=abc123"
    result = redact_secrets(text)
    assert "abc123" not in result


def test_generic_api_key():
    text = "api_key=abc123xyz"
    result = redact_secrets(text)
    assert "abc123xyz" not in result


def test_generic_token():
    text = "token=eyJhbGciOiJIUzI1NiJ9"
    result = redact_secrets(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in result


# --- Idempotency ---

def test_idempotent_anthropic():
    text = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz1234"
    once = redact_secrets(text)
    assert redact_secrets(once) == once


def test_idempotent_mongodb():
    text = "mongodb://user:s3cr3t@host/db"
    once = redact_secrets(text)
    assert redact_secrets(once) == once


def test_idempotent_generic():
    text = "password=hunter2"
    once = redact_secrets(text)
    assert redact_secrets(once) == once


# --- No false positives ---

def test_no_false_positive_prose():
    text = "The quick brown fox jumps over the lazy dog."
    assert redact_secrets(text) == text


def test_no_false_positive_short_sk():
    # sk- followed by fewer than 20 alphanum chars should not be redacted
    text = "sk-shortkey"
    assert redact_secrets(text) == text


def test_empty_string():
    assert redact_secrets("") == ""


def test_no_false_positive_token_in_prose():
    # "token" without an = assignment should not be touched
    text = "the token of appreciation"
    assert redact_secrets(text) == text


def test_no_false_positive_code_variable_name():
    # "secret" as part of a variable name with no key=value pattern
    text = "secret_manager = SecretManager()"
    assert redact_secrets(text) == text


def test_env_style_openai_key():
    # .env-style line — generic assignment AND sk-proj key both present
    text = "OPENAI_API_KEY=sk-proj-AbCdEfGhIjKlMnOpQrStUvWx123456"
    result = redact_secrets(text)
    assert "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx123456" not in result
    assert "[REDACTED]" in result


def test_idempotent_env_style():
    text = "OPENAI_API_KEY=sk-proj-AbCdEfGhIjKlMnOpQrStUvWx123456"
    once = redact_secrets(text)
    assert redact_secrets(once) == once


# --- MCP_REDACT_PATTERNS (user-defined patterns) ---

def test_user_patterns_compiled(monkeypatch):
    monkeypatch.setenv("MCP_REDACT_PATTERNS", r"MYCO-[A-Z0-9]{8}")
    patterns = _load_user_patterns()
    assert len(patterns) == 1
    assert patterns[0].pattern == r"MYCO-[A-Z0-9]{8}"


def test_user_patterns_multiple(monkeypatch):
    monkeypatch.setenv("MCP_REDACT_PATTERNS", r"MYCO-[A-Z0-9]{8}|Bearer [A-Za-z0-9._-]+")
    patterns = _load_user_patterns()
    assert len(patterns) == 2


def test_user_patterns_invalid_skipped(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("MCP_REDACT_PATTERNS", r"GOOD-[A-Z]+|[invalid")
    with caplog.at_level(logging.WARNING, logger="redact"):
        patterns = _load_user_patterns()
    assert len(patterns) == 1  # only valid pattern compiled
    assert "invalid" in caplog.text.lower() or "skipped" in caplog.text.lower()


def test_user_patterns_empty_env(monkeypatch):
    monkeypatch.delenv("MCP_REDACT_PATTERNS", raising=False)
    patterns = _load_user_patterns()
    assert patterns == []
