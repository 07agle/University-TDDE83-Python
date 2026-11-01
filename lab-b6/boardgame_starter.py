"""Brädspel (Fyra i rad) – frivillig utdelad fil för slutprojektet.

GameBoard är densamma som i labbtexten. GameState har bara en docstring –
fälten väljer ni. Funktionerna är kontrakt: signatur och docstring finns,
men kropparna skriver ni. Funktionerna som letar efter fyra i rad, avgör
om planen är full och gör ett drag finns inte här; dem utformar ni själva,
enligt labbtexten.
Rad 0 är brädets översta rad, och en pjäs faller till den lägsta lediga
raden i sin kolumn.
"""

from dataclasses import dataclass


@dataclass
class GameBoard:
    """Spelplanen."""

    rows: int
    cols: int
    grid: list[list[str]]  # " " för tom, "X"/"O" för spelare


@dataclass
class GameState:
    """Hela spelets tillstånd.

    Håller reda på spelplanen, vems tur det är, vilka drag som har gjorts,
    om spelet är slut och vem som i så fall vann. Fälten och deras typer
    bestämmer ni.
    """


def create_board(rows: int = 6, cols: int = 7) -> GameBoard:
    """Skapar en tom spelplan.

    Args:
        rows: Antal rader.
        cols: Antal kolumner.

    Returns:
        En spelplan där varje ruta är " ".
    """
    ...


def new_game(rows: int = 6, cols: int = 7) -> GameState:
    """Skapar ett nytt spel där "X" börjar.

    Args:
        rows: Antal rader.
        cols: Antal kolumner.

    Returns:
        Ett spel med tom plan, där inga drag är gjorda och ingen har vunnit.
    """
    ...


def drop_piece(board: GameBoard, col: int, player: str) -> int | None:
    """Släpper en pjäs i en kolumn.

    Args:
        board: Spelplanen, som ändras om draget är tillåtet.
        col: Kolumnen, räknad från 0.
        player: "X" eller "O".

    Returns:
        Raden där pjäsen hamnade, eller None om kolumnen inte finns eller
        är full. Då ändras ingenting.

    Examples:
        >>> board = create_board(2, 3)
        >>> drop_piece(board, 1, "X"), drop_piece(board, 1, "O")
        (1, 0)
        >>> drop_piece(board, 1, "X") is None
        True
        >>> drop_piece(board, 3, "X") is None
        True
    """
    ...


def format_board(board: GameBoard) -> str:
    """Gör om spelplanen till text att skriva ut i terminalen.

    Varje rad blir en textrad där rutorna skiljs åt av "|". Sista raden
    visar kolumnnumren räknade från 1, som spelaren skriver in.

    Args:
        board: Spelplanen.

    Returns:
        Texten, med raderna åtskilda av radbrytningar.
    """
    ...


def state_to_json(state: GameState) -> str:
    """Gör om hela speltillståndet till JSON-text som går att spara.

    JSON har inga tupler. Sparar ni dragen som tupler skrivs de som listor.

    Args:
        state: Spelet att spara.

    Returns:
        JSON-text med ett JSON-objekt, som state_from_json kan läsa tillbaka.

    Examples:
        >>> import json
        >>> state = new_game()
        >>> type(json.loads(state_to_json(state)))
        <class 'dict'>
        >>> json.loads(state_to_json(state)) != {}
        True
        >>> state_to_json(new_game(2, 3)) != state_to_json(new_game(3, 2))
        True
        >>> state_from_json(state_to_json(state)) == state
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
