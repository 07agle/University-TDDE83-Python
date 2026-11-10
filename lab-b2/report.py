"""Generera textrapporter om datarensningsprocessen.

Denna modul skapar läsbara rapporter som visar vad som gjordes
under rensningsprocessen.
"""

from model import (
    EMAIL,
    PROGRAM,
    REGISTRATION_DATE,
    STUDENT_ID,
    StudentList,
    StudentRecord,
)


def generate_report(
    original_count: int,
    cleaned_count: int,
    duplicates_removed: int,
    fields_fixed: int,
    dates_fixed: int,
) -> str:
    """Genererar en läsbar rapport om rensningsprocessen.

    Rapporten anger alla fem talen och, för vart och ett, vad talet
    betyder. Layouten väljer ni själva.

    Args:
        original_count: Antal poster före rensning
        cleaned_count: Antal poster efter rensning
        duplicates_removed: Antal dubbletter som togs bort
        fields_fixed: Antal poster där fält fylldes i
        dates_fixed: Antal poster där datum normaliserades

    Returns:
        Rapporten som flerradig sträng

    >>> report = generate_report(100, 85, 15, 20, 7)
    >>> [str(number) in report for number in [100, 85, 15, 20, 7]]
    [True, True, True, True, True]
    >>> len(report.splitlines()) > 1
    True
    >>> print(generate_report(1, 1, 1, 1, 1))
    Antal originella poster: 1
    Antal rensade poster: 1
    Antal dubbletter bortagna: 1
    Antal poster där fält fylldes i: 1
    Antal poster där datum normaliserades: 1
    """
    return (
        f"Antal originella poster: {original_count}\n"
        f"Antal rensade poster: {cleaned_count}\n"
        f"Antal dubbletter bortagna: {duplicates_removed}\n"
        f"Antal poster där fält fylldes i: {fields_fixed}\n"
        f"Antal poster där datum normaliserades: {dates_fixed}"

    )

def count_issues(original: StudentList, cleaned: StudentList) -> dict[str, int]:
    """Räknar antal problem som åtgärdades.

    Jämför originaldata med rensat data och räknar tre saker:

    - duplicates_removed: antal poster som togs bort för att samma
      student_id redan fanns tidigare i originaldata
    - fields_fixed: antal poster (inte fält) där epost eller program var
      tomt eller saknades i originaldata men har ett värde i rensat data
    - dates_fixed: antal poster vars registreringsdatum ändrades av
      normaliseringen, både datum i ett annat format som skrevs om och
      ogiltiga eller tomma datum som ersattes med DEFAULT_DATE

    Saknade fält, tom sträng och enbart blanktecken räknas som tomt.
    En borttagen dubblett räknas bara i duplicates_removed.
    Funktionen förutsätter att cleaned är resultatet av rensningen av
    original, så att de enda poster som saknas är borttagna dubbletter.

    Args:
        original: Originaldata
        cleaned: Rensat data

    Returns:
        Dictionary med statistik

    >>> original = [
    ...     {"student_id": "s1", "epost": "", "registreringsdatum": "15/01/2024"},
    ...     {"student_id": "s1", "epost": "", "registreringsdatum": "15/01/2024"},
    ...     {"student_id": "s2", "epost": "erik@student.liu.se",
    ...      "registreringsdatum": "2024-01-16"},
    ... ]
    >>> cleaned = [
    ...     {"student_id": "s1", "epost": "saknas@student.liu.se",
    ...      "registreringsdatum": "2024-01-15", "program": "OKÄNT"},
    ...     {"student_id": "s2", "epost": "erik@student.liu.se",
    ...      "registreringsdatum": "2024-01-16", "program": "OKÄNT"},
    ... ]
    >>> count_issues(original, cleaned)
    {'duplicates_removed': 1, 'fields_fixed': 2, 'dates_fixed': 1}
    >>> count_issues(cleaned, cleaned)
    {'duplicates_removed': 0, 'fields_fixed': 0, 'dates_fixed': 0}
    """
    duplicates_removed = len(original) - len(cleaned)
    fields_fixed = 0
    dates_fixed = 0
    for cleaned_record in cleaned:
        student_id = cleaned_record.get(STUDENT_ID, "")
        # Den första posten med samma id i originaldata är den som behölls.
        original_record: StudentRecord | None = None
        for record in original:
            if record.get(STUDENT_ID, "") == student_id:
                original_record = record
                break
        if original_record is None:
            continue

        for field in [EMAIL, PROGRAM]:
            if not original_record.get(field, "").strip() and cleaned_record.get(field):
                fields_fixed += 1
                break
        old_date = original_record.get(REGISTRATION_DATE, "")
        if old_date != cleaned_record.get(REGISTRATION_DATE, ""):
            dates_fixed += 1

    return {
        "duplicates_removed": duplicates_removed,
        "fields_fixed": fields_fixed,
        "dates_fixed": dates_fixed,
    }
