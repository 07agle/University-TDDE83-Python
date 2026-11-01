"""Campus Planner – frivillig utdelad fil för slutprojektet.

Room är densamma som i labbtexten. Building har bara en docstring – fälten
väljer ni. Funktionerna är kontrakt: signatur och docstring finns, men
kropparna skriver ni. Funktionerna som söker efter ett rum och listar en
byggnads rum finns inte här; dem utformar ni själva, enligt labbtexten.
Datafilen buildings.json har samma format som labbtextens exempel.
"""

from dataclasses import dataclass


@dataclass
class Room:
    """Ett rum i en byggnad."""

    room_id: str
    name: str
    building_id: str
    floor: int
    capacity: int


@dataclass
class Building:
    """En byggnad: dess id, namn och position på kartan, och rummen i den.

    Fälten och deras typer bestämmer ni.
    """


EXAMPLE_JSON = """[
  {"building_id": "b1", "name": "B-huset", "position": [10.5, 20.3],
   "rooms": [
     {"room_id": "b1-001", "name": "Sal 001", "building_id": "b1", "floor": 0, "capacity": 100},
     {"room_id": "b1-101", "name": "Sal 101", "building_id": "b1", "floor": 1, "capacity": 50}
   ]}
]"""


def parse_buildings(json_text: str) -> list[Building]:
    """Tolkar byggnader ur JSON-text i samma format som buildings.json.

    Texten är en lista med byggnader. Varje byggnad har nycklarna
    building_id, name, position och rooms, och varje rum har nycklarna
    room_id, name, building_id, floor och capacity.

    Args:
        json_text: Hela filens innehåll som en sträng.

    Returns:
        Byggnaderna i samma ordning som i texten.

    Examples:
        >>> buildings = parse_buildings(EXAMPLE_JSON)
        >>> len(buildings)
        1
    """
    ...


def load_buildings(path: str) -> list[Building]:
    """Läser byggnader från en JSON-fil, till exempel buildings.json.

    Args:
        path: Sökväg till filen.

    Returns:
        Byggnaderna i filen, tolkade med parse_buildings.
    """
    ...


def buildings_to_json(buildings: list[Building]) -> str:
    """Gör om byggnader till JSON-text i samma format som buildings.json.

    Args:
        buildings: Byggnaderna att skriva.

    Returns:
        JSON-text som parse_buildings kan läsa tillbaka.

    Examples:
        >>> import json
        >>> buildings = parse_buildings(EXAMPLE_JSON)
        >>> json.loads(buildings_to_json(buildings)) == json.loads(EXAMPLE_JSON)
        True
        >>> parse_buildings(buildings_to_json(buildings)) == buildings
        True
    """
    ...


def save_buildings(buildings: list[Building], path: str) -> None:
    """Skriver byggnaderna till en JSON-fil. En befintlig fil skrivs över.

    Args:
        buildings: Byggnaderna att skriva.
        path: Sökväg till filen.
    """
    ...
