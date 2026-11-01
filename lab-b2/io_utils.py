"""Funktioner för att läsa och skriva CSV-filer och textrapporter.

Denna modul hanterar all filhantering för datarensningssystemet.
"""

import csv

from model import StudentList


def read_csv(filename: str) -> StudentList:
    """Läser en CSV-fil och returnerar en lista med dictionaries.

    Första raden tolkas som header med fältnamn. Tomma rader hoppas över.

    Args:
        filename: Sökväg till CSV-filen

    Returns:
        Lista med dictionaries, en per datarad

    >>> from pathlib import Path
    >>> csv_file = Path("read_csv_doctest.csv")
    >>> csv_file.exists()  # doctest: +FAIL_FAST
    False
    >>> _ = csv_file.write_text("student_id,namn\\ns1,Anna\\ns2,Erik\\n", encoding="utf-8")
    >>> read_csv(str(csv_file)) == [
    ...     {'student_id': 's1', 'namn': 'Anna'},
    ...     {'student_id': 's2', 'namn': 'Erik'},
    ... ]
    True
    >>> _ = csv_file.unlink()
    """
    ...


def write_csv(filename: str, records: StudentList, fieldnames: list[str]) -> None:
    """Skriver en lista med dictionaries till en CSV-fil.

    Args:
        filename: Sökväg till utdatafilen
        records: Lista med dictionaries att skriva
        fieldnames: Ordnad lista med fältnamn för header

    >>> from pathlib import Path
    >>> csv_file = Path("write_csv_doctest.csv")
    >>> csv_file.exists()  # doctest: +FAIL_FAST
    False
    >>> write_csv(str(csv_file), [{"student_id": "s1", "namn": "Anna"}], ["student_id", "namn"])
    >>> read_csv(str(csv_file)) == [{'student_id': 's1', 'namn': 'Anna'}]
    True
    >>> _ = csv_file.unlink()
    """
    ...


def write_report(filename: str, content: str) -> None:
    """Skriver en textrapport till fil.

    Args:
        filename: Sökväg till rapporten
        content: Textinnehåll att skriva

    >>> from pathlib import Path
    >>> report_file = Path("write_report_doctest.txt")
    >>> report_file.exists()  # doctest: +FAIL_FAST
    False
    >>> write_report(str(report_file), "Klar")
    >>> report_file.read_text(encoding="utf-8")
    'Klar'
    >>> _ = report_file.unlink()
    """
    ...
