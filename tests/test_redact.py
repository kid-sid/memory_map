import pytest
from redact import redact_secrets


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
