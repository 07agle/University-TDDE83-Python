"""Kommandoradsverktyg för att söka i loggfiler.

Användning:
    uv run python main.py                    # Visa alla loggar
    uv run python main.py --level ERROR      # Visa endast ERROR-loggar
    uv run python main.py --user kenji       # Visa loggar för en operatör
    uv run python main.py --module power --level ERROR  # Kombinera villkor
    uv run python main.py --level "ERROR,WARNING"  # ERROR eller WARNING
"""

import sys

from aggregator import summarize
from log_io import load_logs
from matcher import filter_logs


def parse_args(args: list[str]) -> dict[str, str]:
    """Tolkar kommandoradsargument till ett mönster.

    Argument anges som --fält värde, t.ex. --level ERROR. Ett argument
    som inte börjar med -- hoppas över, liksom ett sista --fält som
    saknar värde. Ett --fält som följs direkt av ett annat --fält tar
    det som värde.

    Args:
        args: Lista med kommandoradsargument (sys.argv[1:])

    Returns:
        Dictionary med fält -> värde (mönster)

    Examples:
        >>> parse_args(["--level", "ERROR", "--module", "power"])
        {'level': 'ERROR', 'module': 'power'}
        >>> parse_args([])
        {}
    """
    pattern: dict[str, str] = {}
    i = 0
    while i < len(args) - 1:
        if args[i].startswith("--"):
            pattern[args[i][2:]] = args[i + 1]
            i += 2
        else:
            i += 1
    return pattern


def main() -> None:
    """Huvudprogram."""
    ...


if __name__ == "__main__":
    main()
