"""Enhetstester för matcher-modulen.

Kör med: uv run pytest test_matcher.py -v
"""

from matcher import filter_logs, match_pattern


def test_match_exact_single_field() -> None:
    """Test: Exakt matchning på ett fält."""
    log = {"level": "ERROR", "module": "auth"}
    assert match_pattern(log, {"level": "ERROR"})
    assert not match_pattern(log, {"level": "INFO"})


def test_match_wildcard_with_none() -> None:
    """Test: Wildcard matchar INTE None."""
    log = {"level": "ERROR", "user": None}
    assert match_pattern(log, {"user": "&"}) is False


def test_filter_logs_empty_pattern() -> None:
    """Test: Tomt mönster matchar alla loggar."""
    logs = [{"level": "ERROR"}, {"level": "INFO"}]
    result = filter_logs(logs, {})
    assert result == logs
