"""Funktioner för att rensa och normalisera data.

Denna modul innehåller alla transformationer som appliceras på
rådata för att producera ren, konsistent data.
"""

from datetime import datetime, date

from model import (
    DEFAULT_DATE,
    DEFAULT_EMAIL,
    DEFAULT_PROGRAM,
    EMAIL,
    PROGRAM,
    REGISTRATION_DATE,
    STUDENT_ID,
    StudentList,
    StudentRecord,
)

from copy import deepcopy


def remove_duplicates(records: StudentList) -> StudentList:
    """Tar bort dubbletter baserat på student_id.

    Om samma student_id förekommer flera gånger, behålls första
    förekomsten.

    Args:
        records: Lista med studentposter (kan innehålla dubbletter)

    Returns:
        Lista utan dubbletter

    >>> data = [
    ...     {"student_id": "s1", "namn": "Anna"},
    ...     {"student_id": "s2", "namn": "Erik"},
    ...     {"student_id": "s1", "namn": "Anna Andersson"}
    ... ]
    >>> remove_duplicates(data)
    [{'student_id': 's1', 'namn': 'Anna'}, {'student_id': 's2', 'namn': 'Erik'}]
    """

    unique_list = []
    student_posts = []
    for record in records:
        if record.get("student_id"):
            if record.get("student_id") not in student_posts:
                student_posts.append(record.get("student_id"))
                unique_list.append(record)
    return unique_list

def _deepcopy_list(studen_list : StudentList) -> StudentList:
    return deepcopy(studen_list)


def fill_missing_fields(records: StudentList) -> StudentList:
    """Fyller i saknade eller tomma fält med default-värden.

    Fälten epost och program ersätts med standardvärden från model.py
    (DEFAULT_EMAIL och DEFAULT_PROGRAM) om de saknas, är tomma strängar
    eller bara innehåller blanktecken.

    Args:
        records: Lista med studentposter

    Returns:
        Lista där alla poster har värden i epost och program

    >>> data = [{"student_id": "s1", "namn": "Anna", "epost": "", "program": "D"}]
    >>> result = fill_missing_fields(data)
    >>> result[0]["epost"]
    'saknas@student.liu.se'
    >>> data[0]["epost"]
    ''
    """
    new_student_list = _deepcopy_list(records)

    for record in new_student_list:
        if not record.get("epost"):
            record["epost"] = DEFAULT_EMAIL
        if not record.get("program"):
            record["program"] = DEFAULT_PROGRAM

    return new_student_list

def _parse_date(date_str: str) -> str:
    """Hjälpfunktion för att tolka datum i olika format.

    Args:
        date_str: Datumsträng i okänt format

    Returns:
        Datum i ISO 8601-format eller DEFAULT_DATE vid fel
    """

    format_list = ["%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"]

    for format in format_list:
        try:
            reg_date = datetime.strptime(date_str, format)
            return reg_date.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return DEFAULT_DATE

def normalize_dates(records: StudentList) -> StudentList:
    """Normaliserar datum till ISO 8601-format (YYYY-MM-DD).

    Hanterar flera indataformat:
    - ISO 8601: 2024-01-15 (behålls oförändrat)
    - Slash-ISO: 2024/01/16 -> 2024-01-16
    - Europeiskt: 15/01/2024 -> 2024-01-15
    - Ogiltiga värden ersätts med DEFAULT_DATE

    Args:
        records: Lista med studentposter

    Returns:
        Ny lista med normaliserade datum; posterna i records ändras inte

    >>> data = [
    ...     {"student_id": "s1", "registreringsdatum": "15/01/2024"},
    ...     {"student_id": "s2", "registreringsdatum": "2024/02/03"}
    ... ]
    >>> result = normalize_dates(data)
    >>> result[0]["registreringsdatum"]
    '2024-01-15'
    >>> result[1]["registreringsdatum"]
    '2024-02-03'
    >>> data[0]["registreringsdatum"]
    '15/01/2024'
    """
    new_student_list = deepcopy(records)

    for record in new_student_list:
        if record.get("registreringsdatum"):
            record["registreringsdatum"] = _parse_date(record.get("registreringsdatum"))
    return new_student_list


