"""Textäventyr – frivillig utdelad fil för slutprojektet.

Room och GameState är desamma som i labbtexten. Player har bara en
docstring – fälten väljer ni. Funktionerna är kontrakt: signatur och
docstring finns, men kropparna skriver ni. Funktionerna som flyttar
spelaren och plockar upp och släpper föremål finns inte här; dem utformar
ni själva, enligt labbtexten.
Datafilen world.json har samma format som labbtextens exempel.
"""

from dataclasses import dataclass


@dataclass
class Room:
    """Ett rum i spelvärlden."""

    room_id: str
    name: str
    description: str
    exits: dict[str, str]  # riktning -> room_id
    items: list[str]


@dataclass
class Player:
    """Var spelaren är, vad hen bär och var hen har varit.

    Fälten och deras typer bestämmer ni.
    """


@dataclass
class GameState:
    """Hela spelets tillstånd."""

    player: Player
    rooms: dict[str, Room]
    message_log: list[str]


EXAMPLE_JSON = """{"rooms": [
  {"room_id": "start", "name": "Entréhallen",
   "description": "En stor hall med stenmurar. En fackla fladdrar på väggen.",
   "exits": {"norr": "bibliotek", "öst": "korridor"}, "items": ["fackla"]},
  {"room_id": "bibliotek", "name": "Det gamla biblioteket",
   "description": "Dammiga hyllor fyllda med böcker. En mystisk karta ligger på bordet.",
   "exits": {"söder": "start", "öst": "skattkammaren"}, "items": ["karta"]}
]}"""


def parse_world(json_text: str) -> dict[str, Room]:
    """Tolkar spelvärlden ur JSON-text i samma format som world.json.

    Texten är ett objekt med nyckeln rooms, vars värde är en lista med rum.
    Varje rum har nycklarna room_id, name, description, exits och items.

    Args:
        json_text: Hela filens innehåll som en sträng.

    Returns:
        En dictionary från room_id till rummet.

    Examples:
        >>> rooms = parse_world(EXAMPLE_JSON)
        >>> sorted(rooms)
        ['bibliotek', 'start']
        >>> rooms["start"].exits["norr"], rooms["start"].items
        ('bibliotek', ['fackla'])
    """
    ...


def load_world(path: str) -> dict[str, Room]:
    """Läser spelvärlden från en JSON-fil, till exempel world.json.

    Args:
        path: Sökväg till filen.

    Returns:
        Rummen i filen, tolkade med parse_world.
    """
    ...


def new_game(rooms: dict[str, Room], start_room: str) -> GameState:
    """Skapar ett nytt spel där spelaren står i start_room.

    Args:
        rooms: Spelvärlden.
        start_room: room_id för rummet spelaren börjar i.

    Returns:
        Ett spel där spelaren inte bär något, har besökt bara startrummet
        och meddelandeloggen är tom.
    """
    ...


def state_to_json(state: GameState) -> str:
    """Gör om hela speltillståndet till JSON-text som går att spara.

    JSON har inga mängder (set). Använder ni mängder skrivs de som listor.

    Args:
        state: Spelet att spara.

    Returns:
        JSON-text med ett JSON-objekt, som state_from_json kan läsa tillbaka.

    Examples:
        >>> import json
        >>> state = new_game(parse_world(EXAMPLE_JSON), "start")
        >>> type(json.loads(state_to_json(state)))
        <class 'dict'>
        >>> "start" in state_to_json(state)
        True
        >>> state_from_json(state_to_json(state)) == state
        True
        >>> moved = new_game(parse_world(EXAMPLE_JSON), "bibliotek")
        >>> moved.message_log.append("Du gick norrut.")
        >>> state_to_json(moved) != state_to_json(state)
        True
        >>> state_from_json(state_to_json(moved)) == moved
        True
    """
    ...


def state_from_json(json_text: str) -> GameState:
    """Återskapar ett speltillstånd ur text från state_to_json.

    Args:
        json_text: Text som state_to_json har skapat.

    Returns:
        Speltillståndet, likadant som när det sparades.
    """
    ...
