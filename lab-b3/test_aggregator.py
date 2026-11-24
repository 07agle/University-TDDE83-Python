"""Enhetstester för aggregator-modulen.

Kör med: uv run pytest test_aggregator.py -v
"""

from aggregator import count_by, format_counts, level_key, module_key


def test_count_by_single_value() -> None:
    """Test: Alla poster har samma nivå."""
    logs = [{"level": "ERROR"}, {"level": "ERROR"}, {"level": "ERROR"}]
    result = count_by(logs, level_key)
    assert result == {"ERROR": 3}


def test_format_counts_simple() -> None:
    """Test: Formatera enkla räkningsresultat."""
    counts = {"ERROR": 5, "INFO": 10}
    result = format_counts(counts, "Test")
    expected = "Test:\n  ERROR: 5\n  INFO: 10"
    assert result == expected


def test_module_key_no_value() -> None:
    """Test: Kontrollerar om värdet None ger '(inget värde)'"""
    assert module_key({"module": "power"}) != "(inget värde)"

def test_count_by_module_key() -> None:
    """Test: Kontrollerar count_by med module key med flera logs"""
    logs = [{"module": "comms"}, {"module": "power"}, {"module": "comms"}]
    result = count_by(logs, module_key)
    assert result == {"power": 1, "comms": 2}

