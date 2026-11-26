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


def test_match_missing_field() -> None:
    """Ett mönster som frågar efter ett fält som loggposten saknar matchar inte."""
    log = {"module": "auth"}
    assert not match_pattern(log, {"level": "ERROR"})


def test_match_alternatives_hit() -> None:
    """En loggpost vars level är ett av alternativen i "ERROR,WARNING" matchar."""
    log = {"level": "ERROR", "user": None}
    assert match_pattern(log, {"level": "ERROR,WARNING"})


def test_filter_logs_returns_only_matches() -> None:
    """Av flera loggposter returnerar filter_logs precis de som matchar, och alla som returneras har rätt värde."""
    logs = [{"level": "ERROR"}, {"level": "INFO"}]
    assert filter_logs(logs, {"level": "ERROR"}) == [{"level": "ERROR"}]


def test_match_pattern_wildcard() -> None:
    logs = {"level": "ERROR", "user": None, "module": "power"}
    assert match_pattern(logs, {"level": "&"})


def test_filter_logs_wildcard() -> None:
    logs = [
        {"level": "ERROR", "user": None, "module": "power"},
        {"level": "INFO", "user": "omar"},
    ]
    assert filter_logs(logs, {"level": "&"}) == [
        {"level": "ERROR", "user": None, "module": "power"},
        {"level": "INFO", "user": "omar"},
    ]

