"""Läsa JSON Lines-filer (JSONL).

JSON Lines (JSONL) är ett format där varje rad är ett komplett JSON-objekt.
Formatet är vanligt för loggfiler eftersom det är enkelt att lägga till
nya rader sist i filen utan att behöva läsa om hela filen.
"""

import json

type LogEntry = dict[str, str | None]
type LogList = list[LogEntry]


def load_logs(filename: str) -> LogList:
    """Läser en JSON Lines-fil och returnerar lista med loggposter.

    Varje rad i filen ska vara ett giltigt JSON-objekt. Tomma rader
    hoppas över. Felaktiga JSON-rader loggas som varning men stoppar
    inte inläsningen.

    Args:
        filename: Sökväg till JSONL-filen

    Returns:
        Lista med loggposter som dictionaries

    Raises:
        FileNotFoundError: Om filen inte finns
    """
    log_list = []
    with open(filename, "r", encoding="utf-8") as f:
        for row in f:
            try:
                if row.strip():
                    log_list.append(json.loads(row))
            except json.decoder.JSONDecodeError:
                print("Felaktig JSON-rad")
    return log_list
