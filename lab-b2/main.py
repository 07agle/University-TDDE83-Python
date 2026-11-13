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
    student_list = read_csv("studentdata.csv")
    
    cleaned_list = remove_duplicates(student_list)
    cleaned_list = fill_missing_fields(cleaned_list)
    cleaned_list = normalize_dates(cleaned_list)

    header = ["student_id", "namn", "epost", "registreringsdatum", "program"]

    write_csv("studentdata_clean.csv", cleaned_list, header)

    issues_dict = count_issues(student_list, cleaned_list)
    original_count = len(student_list)
    cleaned_count = len(cleaned_list)
    duplicated_removed = issues_dict.get("duplicates_removed")
    fields_fixed = issues_dict.get("fields_fixed")
    dates_fixed = issues_dict.get("dates_fixed")

    write_report(
        "rensningsrapport.txt",
        generate_report(
            original_count, cleaned_count, duplicated_removed, fields_fixed, dates_fixed
        ),
    )


if __name__ == "__main__":
    main()
