"""Mönstermatchning för loggfiler.

Den här modulen implementerar ett flexibelt mönsterspråk för att matcha
loggposter baserat på innehåll och struktur.
"""

from log_io import LogEntry, LogList

type Pattern = dict[str, str]


def match_pattern(log_entry: LogEntry, pattern: Pattern) -> bool:
    """Kontrollerar om en loggpost matchar ett givet mönster.

    Matchningsregler:
    1. Alla fält i mönstret måste finnas i loggposten
    2. Värden måste matcha exakt, UTOM:
       - "&" i mönstret matchar vilket värde som helst (men inte None/null)
       - ett mönstervärde med komma, t.ex. "ERROR,WARNING", matchar om
         loggpostens värde är något av alternativen

    Args:
        log_entry: Loggpost att testa
        pattern: Mönster att matcha mot

    Returns:
        True om loggposten matchar mönstret, annars False

    Examples:
        >>> entry = {"level": "ERROR", "module": "auth", "user": "anna"}
        >>> match_pattern(entry, {"level": "ERROR"})
        True
        >>> match_pattern(entry, {"level": "INFO"})
        False
        >>> match_pattern(entry, {"level": "ERROR", "module": "auth"})
        True
        >>> match_pattern(entry, {"level": "ERROR", "module": "db"})
        False
        >>> match_pattern(entry, {"user": "&"})
        True
        >>> match_pattern(entry, {"level": "ERROR,WARNING"})
        True
        >>> match_pattern(entry, {"level": "INFO,DEBUG"})
        False
    """
    ...


def filter_logs(logs: LogList, pattern: Pattern) -> LogList:
    """Filtrerar loggar baserat på ett mönster.

    Args:
        logs: Lista med loggposter
        pattern: Mönster att filtrera på

    Returns:
        Lista med loggposter som matchar mönstret

    Examples:
        >>> logs = [
        ...     {"level": "ERROR", "msg": "Failed"},
        ...     {"level": "INFO", "msg": "OK"},
        ...     {"level": "ERROR", "msg": "Timeout"}
        ... ]
        >>> filter_logs(logs, {"level": "ERROR"})
        [{'level': 'ERROR', 'msg': 'Failed'}, {'level': 'ERROR', 'msg': 'Timeout'}]
    """
    ...
