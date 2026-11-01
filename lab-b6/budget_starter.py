"""Personal Finance Tracker – frivillig utdelad fil för slutprojektet.

Transaction är densamma som i labbtexten. MonthlySummary har bara en
docstring – fälten väljer ni. Funktionerna är kontrakt: signatur och
docstring finns, men kropparna skriver ni. Funktionerna som summerar per
kategori och sammanställer en månad finns inte här; dem utformar ni
själva, enligt labbtexten.
Datafilen transactions.csv har samma format som labbtextens exempel.
"""

from dataclasses import dataclass
from datetime import date


@dataclass
class Transaction:
    """En utgift."""

    transaction_id: str
    date: date
    amount: float
    category: str  # mat, transport, nöje, etc.
    description: str
    payment_method: str


@dataclass
class MonthlySummary:
    """Utgifterna under en månad.

    Visar vilken månad det gäller, den totala summan, summan per kategori
    och hur många transaktioner månaden har. Fälten och deras typer
    bestämmer ni.
    """


EXAMPLE_CSV = """transaction_id,date,amount,category,description,payment_method
tx001,2024-03-01,350.00,mat,ICA Maxi,kort
tx002,2024-03-02,50.00,transport,Bussbiljett,kort
tx003,2024-03-05,1200.00,nöje,Konsertbiljetter,kort
"""


def parse_transactions(csv_text: str) -> list[Transaction]:
    """Tolkar transaktioner ur CSV-text i samma format som transactions.csv.

    Första raden är rubrikraden transaction_id,date,amount,category,
    description,payment_method. Kolumnerna skiljs åt med kommatecken,
    datumet är i ISO-format (ÅÅÅÅ-MM-DD) och beloppet har punkt som
    decimaltecken.

    Args:
        csv_text: Hela filens innehåll som en sträng.

    Returns:
        Transaktionerna i samma ordning som i texten.

    Examples:
        >>> transactions = parse_transactions(EXAMPLE_CSV)
        >>> len(transactions)
        3
        >>> transactions[0].date, transactions[0].amount
        (datetime.date(2024, 3, 1), 350.0)
    """
    ...


def load_transactions(path: str) -> list[Transaction]:
    """Läser transaktioner från en CSV-fil, till exempel transactions.csv.

    Args:
        path: Sökväg till filen.

    Returns:
        Transaktionerna i filen, tolkade med parse_transactions.
    """
    ...


def largest_expenses(transactions: list[Transaction], n: int) -> list[Transaction]:
    """Väljer ut de n största utgifterna, störst först.

    Args:
        transactions: Transaktionerna att välja bland.
        n: Hur många som ska väljas. Finns färre väljs alla.

    Returns:
        En ny lista. Den ursprungliga listan ändras inte.

    Examples:
        >>> top = largest_expenses(parse_transactions(EXAMPLE_CSV), 2)
        >>> [t.transaction_id for t in top]
        ['tx003', 'tx001']
    """
    ...


def save_summary(summary: MonthlySummary, path: str) -> None:
    """Skriver en månadssammanställning till fil. En befintlig fil skrivs över.

    Vilket format ni väljer (till exempel JSON eller CSV) bestämmer ni,
    men alla fält i MonthlySummary ska finnas med.

    Args:
        summary: Sammanställningen att skriva.
        path: Sökväg till filen.
    """
    ...
