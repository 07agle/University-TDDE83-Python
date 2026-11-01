"""Aggregera och sammanfatta loggdata.

Modulen räknar loggposter med hjälp av en nyckelfunktion och
formaterar resultatet som en rapport.
"""

from collections.abc import Callable

from log_io import LogEntry, LogList

type CountDict = dict[str, int]


def count_by(logs: LogList, key: Callable[[LogEntry], str | None]) -> CountDict:
    """Räknar hur många loggposter som ger varje nyckel.

    Funktionen key anropas med en loggpost i taget och returnerar
    postens nyckel. Returnerar key None räknas posten inte alls.

    Args:
        logs: Lista med loggposter
        key: Funktion som ger en loggposts nyckel, eller None om
            posten inte ska räknas

    Returns:
        Dictionary med nyckel -> antal poster

    Examples:
        >>> logs = [
        ...     {"level": "ERROR", "user": "anna"},
        ...     {"level": "INFO", "user": "erik"},
        ...     {"level": "ERROR", "user": None},
        ...     {"level": None, "user": "lisa"},
        ...     {"user": "omar"},
        ... ]
        >>> count_by(logs, level_key) == {'ERROR': 2, 'INFO': 1, '(inget värde)': 1}
        True
    """
    ...


def level_key(entry: LogEntry) -> str | None:
    """Returnerar loggpostens nivå (level) som nyckel för count_by.

    Saknar posten fältet level returneras None, så att count_by hoppar
    över posten. Har fältet värdet None returneras strängen
    '(inget värde)', så att posten räknas under den nyckeln.

    Args:
        entry: Loggposten

    Returns:
        Postens nivå, '(inget värde)' eller None

    Examples:
        >>> level_key({"level": "ERROR", "user": "anna"})
        'ERROR'
        >>> level_key({"level": None})
        '(inget värde)'
        >>> level_key({"user": "anna"}) is None
        True
    """
    ...


def module_key(entry: LogEntry) -> str | None:
    """Returnerar loggpostens modul (module) som nyckel för count_by.

    Samma regler som för level_key, men för fältet module.

    Args:
        entry: Loggposten

    Returns:
        Postens modul, '(inget värde)' eller None
    """
    ...


def format_counts(counts: CountDict, title: str) -> str:
    """Formaterar räkningsresultat till en läsbar sträng.

    Raderna kommer i samma ordning som i counts.

    Args:
        counts: Dictionary med värde -> antal
        title: Titel för rapporten

    Returns:
        Formaterad sträng med en rad per värde

    Examples:
        >>> counts = {"ERROR": 5, "INFO": 10, "WARNING": 2}
        >>> print(format_counts(counts, "Fördelning per nivå"))
        Fördelning per nivå:
          ERROR: 5
          INFO: 10
          WARNING: 2
    """
    lines = [f"{title}:"]
    for value, count in counts.items():
        lines.append(f"  {value}: {count}")
    return "\n".join(lines)


def summarize(logs: LogList) -> str:
    """Skapar en sammanfattning av loggposterna.

    Sammanfattningen innehåller antalet poster och fördelningen per
    nivå och per modul. En fördelning utan några räknade poster
    utelämnas.

    Args:
        logs: Lista med loggposter

    Returns:
        Formaterad sammanfattning
    """
    parts = [f"Sammanfattning ({len(logs)} poster):"]
    level_counts = count_by(logs, level_key)
    if level_counts:
        parts.append("")
        parts.append(format_counts(level_counts, "Per nivå (level)"))
    module_counts = count_by(logs, module_key)
    if module_counts:
        parts.append("")
        parts.append(format_counts(module_counts, "Per modul (module)"))
    return "\n".join(parts)
