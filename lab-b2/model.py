"""Datamodeller och typaliaser för datarensningssystemet.

Denna modul definierar de datastrukturer som används genom hela
systemet för att representera studentdata och kursdata.

Filen är färdig. Ni ska inte ändra den, bara använda namnen i den.
"""

# Typalias för klarare kod
type StudentRecord = dict[str, str]
type CourseRecord = dict[str, str]
type StudentList = list[StudentRecord]
type CourseList = list[CourseRecord]

# Konstanter för fältnamn (undviker stavfel)
STUDENT_ID = "student_id"
NAME = "namn"
EMAIL = "epost"
REGISTRATION_DATE = "registreringsdatum"
PROGRAM = "program"
COURSE = "kurs"
SEMESTER = "termin"

# Default-värden för saknade fält
DEFAULT_EMAIL = "saknas@student.liu.se"
DEFAULT_PROGRAM = "OKÄNT"
DEFAULT_DATE = "2024-01-01"
DEFAULT_COURSE = "INGEN"
