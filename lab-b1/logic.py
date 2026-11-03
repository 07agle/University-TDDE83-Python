"""Funktioner för att läsa och bearbeta kursregistreringar."""

def read_lines(filename: str) -> list[str]:
    """Läser alla rader från en fil och returnerar dem som en lista.

    Varje rad rensas från omgivande blanktecken (whitespace).

    Args:
        filename: Sökväg till filen som ska läsas

    Returns:
        Lista med rader från filen (rensade från whitespace)

    >>> from pathlib import Path
    >>> test_file = Path("read_lines_doctest.txt")
    >>> test_file.exists()  # doctest: +FAIL_FAST
    False
    >>> _ = test_file.write_text("  Anna\\nErik  \\n", encoding="utf-8")
    >>> read_lines(str(test_file))
    ['Anna', 'Erik']
    >>> _ = test_file.unlink()
    """
    row_list = []
    with open(filename, "r", encoding="utf-8") as f:
        for row in f:
            row_list.append(row.strip())
    return row_list

def parse_record(line: str) -> dict[str, str]:
    """Tolkar en rad med semikolonseparerade fält till en dictionary.

    Förväntat format: fornamn;efternamn;personnummer;kurskod;kursnamn

    Args:
        line: En rad med semikolonseparerade värden

    Returns:
        Dictionary med nycklarna 'fornamn', 'efternamn', 'personnummer',
        'kurskod' och 'kursnamn'

    >>> record = parse_record("Anna;Andersson;980101-1234;TDDE82;Python grundkurs")
    >>> record == {
    ...     'fornamn': 'Anna', 'efternamn': 'Andersson', 'personnummer': '980101-1234',
    ...     'kurskod': 'TDDE82', 'kursnamn': 'Python grundkurs',
    ... }
    True

    >>> record2 = parse_record("Erik;Gruk;930101-1848;TDDE83;Python grundkurs")
    >>> record2 == {
    ...     'fornamn': 'Erik', 'efternamn': 'Gruk', 'personnummer': '930101-1848',
    ...     'kurskod': 'TDDE83', 'kursnamn': 'Python grundkurs',
    ... }
    True
    """
    element_list = line.split(";")

    person_dict = {}
    person_dict["fornamn"] = element_list[0]
    person_dict["efternamn"] = element_list[1]
    person_dict["personnummer"] = element_list[2]
    person_dict["kurskod"] = element_list[3]
    person_dict["kursnamn"] = element_list[4]

    return person_dict

def filter_records(
    records: list[dict[str, str]], field: str, value: str
) -> list[dict[str, str]] | None:
    """Filtrerar poster baserat på ett fält och önskat värde.

    Args:
        records: Lista med poster, där varje post är en dictionary med
            strängar som både nycklar och värden
        field: Namn på fältet att filtrera på
        value: Värdet som fältet ska ha

    Returns:
        Lista med poster där fältet har det angivna värdet

    >>> data = [{'namn': 'Anna', 'kurs': 'TDDE82'}, {'namn': 'Erik', 'kurs': 'TDDE83'}]
    >>> filter_records(data, 'kurs', 'TDDE82')
    [{'namn': 'Anna', 'kurs': 'TDDE82'}]
    >>> filter_records(data, 'kurs', 'TDDE84')
    []
    >>> filter_records(data, 'namn', 'Erik')
    [{'namn': 'Erik', 'kurs': 'TDDE83'}]
    >>> data
    [{'namn': 'Anna', 'kurs': 'TDDE82'}, {'namn': 'Erik', 'kurs': 'TDDE83'}]
    """
    result = []

    for dicter in records:
        for key, values in dicter.items():
            if key == field:
                if values == value:
                    result.append(dicter)

    return result

def summarize(records: list[dict[str, str]]) -> str:
    """Skapar en formaterad sammanfattning av registreringar per kurs.

    Args:
        records: Lista med registreringsposter

    Returns:
        Formaterad sträng med antal registreringar per kurskod

    >>> data = [
    ...     {'fornamn': 'Anna', 'kurskod': 'TDDE82'},
    ...     {'fornamn': 'Erik', 'kurskod': 'TDDE83'},
    ...     {'fornamn': 'Priya', 'kurskod': 'TDDE82'},
    ...     {'fornamn': 'Omar', 'kurskod': 'TDDE83'},
    ...     {'fornamn': 'Maria', 'kurskod': 'TDDE82'}
    ... ]
    >>> print(summarize(data))
    Kursregistreringar:
    TDDE82: 3 studenter
    TDDE83: 2 studenter
    >>> print(summarize([]))
    Kursregistreringar:

    >>> new_data = [
    ...     {'fornamn': 'Anna', 'kurskod': 'TDDE82'},
    ...     {'fornamn': 'Erik', 'kurskod': 'TDDE83'},
    ...     {'fornamn': 'Priya', 'kurskod': 'TDDE83'},
    ...     {'fornamn': 'Omar', 'kurskod': 'TDDE83'},
    ...     {'fornamn': 'Maria', 'kurskod': 'TDDE82'},
    ...     {'fornamn': 'Märtha', 'kurskod': 'TDDD65'},
    ...     {'fornamn': 'Åsa', 'kurskod': 'TDDD65'}
    ... ]
    >>> print(summarize(new_data))
    Kursregistreringar:
    TDDE82: 2 studenter
    TDDE83: 3 studenter
    TDDD65: 2 studenter
    """
    kursreg = {}

    for student in records:
        for key, values in student.items():
            if key == "kurskod":
                if values in kursreg:
                    kursreg[values] += 1
                else:
                    kursreg[values] = 1

    summerized_text = "Kursregistreringar:"

    for key, values in kursreg.items():
        summerized_text += f"\n{key}: {values} studenter"

    return summerized_text
