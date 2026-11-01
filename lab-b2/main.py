"""Huvudprogram för datarensningssystemet.

Detta program läser rådata, applicerar alla rensningssteg i ordning,
och producerar ren data samt en rapport.
"""

from cleaning import fill_missing_fields, normalize_dates, remove_duplicates
from io_utils import read_csv, write_csv, write_report
from model import EMAIL, NAME, PROGRAM, REGISTRATION_DATE, STUDENT_ID
from report import count_issues, generate_report


def main() -> None:
    """Kör alla steg i datarensningen."""
    ...


if __name__ == "__main__":
    main()
