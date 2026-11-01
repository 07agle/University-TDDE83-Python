"""Wordle-klon – frivillig utdelad fil för slutprojektet.

GuessResult och Statistics är desamma som i labbtexten. GameState har bara
en docstring – fälten väljer ni. Funktionerna är kontrakt: signatur och
docstring finns, men kropparna skriver ni. Funktionerna som kontrollerar en
gissning, registrerar den i spelet och visar historiken finns inte här;
dem utformar ni själva, enligt labbtexten.
Datafilen ordlista.txt har ett ord per rad, som labbtextens exempel.
"""

from dataclasses import dataclass


@dataclass
class GuessResult:
    """En gissning och dess feedback, en markering per bokstav."""

    guess: str
    feedback: list[str]  # ["green", "yellow", "gray", "green", "yellow"]


@dataclass
class GameState:
    """Ett pågående eller avslutat spel.

    Håller reda på målordet, gissningarna hittills med sin feedback, hur
    många gissningar spelaren har och om spelet är vunnet eller förlorat.
    Fälten och deras typer bestämmer ni.
    """


@dataclass
class Statistics:
    """Statistik över flera spel."""

    games_played: int
    games_won: int
    current_streak: int
    max_streak: int
    guess_distribution: dict[int, int]  # {1: 5, 2: 12, 3: 20, ...}


WORD_LENGTH = 5


def parse_word_list(text: str) -> list[str]:
    """Tolkar en ordlista med ett ord per rad.

    Orden görs om till versaler. Tomma rader och ord som inte har exakt
    WORD_LENGTH tecken hoppas över.

    Args:
        text: Hela filens innehåll som en sträng.

    Returns:
        Orden i samma ordning som i texten.

    Examples:
        >>> parse_word_list("BANAN\\ncykel\\n\\nFJÄLL\\nKORT\\n")
        ['BANAN', 'CYKEL', 'FJÄLL']
    """
    ...


def load_words(path: str) -> list[str]:
    """Läser en ordlista från fil, till exempel ordlista.txt.

    Args:
        path: Sökväg till filen.

    Returns:
        Orden i filen, tolkade med parse_word_list.
    """
    ...


def generate_feedback(guess: str, target: str) -> list[str]:
    """Beräknar feedback för en gissning mot målordet.

    "green" betyder rätt bokstav på rätt plats, "yellow" att bokstaven
    finns i målordet men på en annan plats, och "gray" att den inte finns.
    En bokstav som förekommer flera gånger i gissningen får bara "green"
    eller "yellow" så många gånger som den förekommer i målordet, och
    "green" delas ut först.

    Args:
        guess: Gissningen, i versaler.
        target: Målordet, i versaler och lika långt som gissningen.

    Returns:
        En markering per bokstav i gissningen.

    Examples:
        >>> generate_feedback("BANAN", "BANAN")
        ['green', 'green', 'green', 'green', 'green']
        >>> generate_feedback("DATOR", "GRODA")
        ['yellow', 'yellow', 'gray', 'yellow', 'yellow']
        >>> generate_feedback("PENNA", "BANAN")
        ['gray', 'gray', 'green', 'yellow', 'yellow']
        >>> generate_feedback("LAMPA", "PENNA")
        ['gray', 'gray', 'gray', 'yellow', 'green']
    """
    ...


def new_game(target_word: str, max_guesses: int = 6) -> GameState:
    """Skapar ett nytt spel utan gissningar.

    Args:
        target_word: Målordet, i versaler.
        max_guesses: Hur många gissningar spelaren har.

    Returns:
        Ett spel utan gissningar, som varken är vunnet eller förlorat.
    """
    ...


def save_result(state: GameState, path: str) -> None:
    """Lägger till spelets resultat sist i en resultatfil.

    Filen skapas om den saknas. Varje spel blir en rad i CSV- eller
    JSONL-format som minst anger om spelet vanns och hur många
    gissningar som användes.

    Args:
        state: Ett avslutat spel.
        path: Sökväg till resultatfilen.
    """
    ...
