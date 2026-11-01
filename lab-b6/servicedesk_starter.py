"""Mini Service Desk – frivillig utdelad fil för slutprojektet.

Ticket är densamma som i labbtexten. TicketStats har bara en docstring –
fälten väljer ni. Funktionerna är kontrakt: signatur och docstring finns,
men kropparna skriver ni. Funktionerna som filtrerar ärenden, byter status
och räknar ärenden finns inte här; dem utformar ni själva, enligt
labbtexten. Datafilen tickets.jsonl har samma format som labbtextens
exempel.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Ticket:
    """Ett supportärende."""

    ticket_id: str
    title: str
    description: str
    status: str  # open, in_progress, closed
    priority: str  # low, medium, high, critical
    created: datetime
    updated: datetime
    assigned_to: str | None = None


@dataclass
class TicketStats:
    """Sammanställning över en samling ärenden.

    Visar hur många ärenden det finns, hur många som har varje status och
    varje prioritet, och den genomsnittliga lösningstiden för de stängda.
    Fälten och deras typer bestämmer ni.
    """


EXAMPLE_LINES = [
    (
        '{"ticket_id": "T001", "title": "Skrivaren funkar inte", "description": "...", '
        '"status": "open", "priority": "medium", "created": "2024-03-15T10:00:00", '
        '"updated": "2024-03-15T10:00:00"}'
    ),
    (
        '{"ticket_id": "T002", "title": "Glömt lösenord", "description": "...", '
        '"status": "closed", "priority": "low", "created": "2024-03-14T09:00:00", '
        '"updated": "2024-03-14T09:30:00"}'
    ),
]


def parse_ticket(line: str) -> Ticket:
    """Tolkar en rad i tickets.jsonl som ett ärende.

    Raden är ett JSON-objekt med nycklarna ticket_id, title, description,
    status, priority, created och updated, och kan ha assigned_to.
    Tidpunkterna är i ISO-format och blir datetime-objekt.

    Args:
        line: En rad ur filen.

    Returns:
        Ärendet. Saknas assigned_to blir det None.

    Examples:
        >>> ticket = parse_ticket(EXAMPLE_LINES[1])
        >>> ticket.ticket_id, ticket.status, ticket.assigned_to
        ('T002', 'closed', None)
        >>> ticket.updated - ticket.created
        datetime.timedelta(seconds=1800)
    """
    ...


def load_tickets(path: str) -> list[Ticket]:
    """Läser alla ärenden från en JSONL-fil, till exempel tickets.jsonl.

    Tomma rader hoppas över.

    Args:
        path: Sökväg till filen.

    Returns:
        Ärendena i filens ordning, tolkade med parse_ticket.
    """
    ...


def ticket_to_line(ticket: Ticket) -> str:
    """Gör om ett ärende till en rad i samma format som tickets.jsonl.

    Args:
        ticket: Ärendet att skriva.

    Returns:
        Ett JSON-objekt på en rad, utan radbrytning på slutet.

    Examples:
        >>> import json
        >>> ticket = parse_ticket(EXAMPLE_LINES[0])
        >>> data = json.loads(ticket_to_line(ticket))
        >>> data["ticket_id"], data["status"], data["created"]
        ('T001', 'open', '2024-03-15T10:00:00')
        >>> parse_ticket(ticket_to_line(ticket)) == ticket
        True
    """
    ...


def save_tickets(tickets: list[Ticket], path: str) -> None:
    """Skriver alla ärenden till en JSONL-fil, ett per rad.

    En befintlig fil skrivs över.

    Args:
        tickets: Ärendena att skriva.
        path: Sökväg till filen.
    """
    ...
