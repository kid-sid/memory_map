import pytest
from memory_map_mcp.server import _compress_memory, _abbreviate, _shorten_key


def test_level_0_raw():
    data = {"stack": "Python FastAPI", "entry_point": "main.py"}
    result = _compress_memory(data, 0)
    assert "stack: Python FastAPI" in result
    assert "entry_point: main.py" in result


def test_level_1_compact():
    data = {"stack": "Python FastAPI"}
    result = _compress_memory(data, 1)
    assert "[stack] Python FastAPI" in result


def test_level_2_dense_key_abbrev():
    data = {"entry_point": "main.py"}
    result = _compress_memory(data, 2)
    assert "[entry]" in result


def test_level_2_dense_value_abbrev():
    data = {"stack": "python application"}
    result = _compress_memory(data, 2)
    assert "py" in result
    assert "app" in result


def test_skips_system_keys():
    data = {"_compression": 2, "stack": "Python"}
    result = _compress_memory(data, 1)
    assert "_compression" not in result
    assert "[stack] Python" in result


def test_empty_returns_placeholder():
    assert _compress_memory({}, 1) == "no memory saved yet"


def test_only_system_keys_returns_placeholder():
    assert _compress_memory({"_compression": 1}, 1) == "no memory saved yet"


def test_abbreviation_python():
    assert "py" in _abbreviate("python application")


def test_abbreviation_database():
    assert "db" in _abbreviate("database connection")


def test_abbreviation_case_insensitive():
    result = _abbreviate("Python JavaScript TypeScript")
    assert "py" in result
    assert "js" in result
    assert "ts" in result


def test_filler_removal():
    result = _abbreviate("the main server is ready")
    assert "the " not in result.lower()


def test_key_shortening_known():
    assert _shorten_key("entry_point") == "entry"
    assert _shorten_key("testing") == "test"
    assert _shorten_key("deployment") == "deploy"


def test_key_shortening_unknown():
    assert _shorten_key("my_custom_key") == "my_custom_key"
