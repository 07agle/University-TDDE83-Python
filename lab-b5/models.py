"""Datamodeller för kalendersystemet.

Den här modulen definierar dataklasser för tidsfönster och händelser.
"""

from dataclasses import dataclass
from datetime import datetime

type EventId = str
type Participant = str


@dataclass(frozen=True)
class TimeSlot:
    """Ett tidsfönster med start- och sluttid.

    Ett tidsfönster är ett värde och går inte att ändra efter att det
    har skapats.

    Attribut:
        start: Starttidpunkt
        end: Sluttidpunkt
    """

    def __post_init__(self) -> None:
        """Validerar att tidsfönstret är giltigt efter skapande."""
        if self.end <= self.start:
            raise ValueError("Sluttid måste vara efter starttid")

    def duration_minutes(self) -> int:
        """Beräknar tidsfönstrets längd i minuter.

        Returns:
            Antalet hela minuter från start till slut.
        """
        ...

    def overlaps(self, other: TimeSlot) -> bool:
        """Kontrollerar om detta tidsfönster överlappar ett annat.

        Två tidsfönster överlappar om de har någon tid gemensamt.
        Tidsfönster som bara möts vid en exakt tidpunkt, där det ena
        slutar när det andra börjar, räknas inte som överlapp.

        Args:
            other: Det andra tidsfönstret att jämföra med

        Returns:
            True om tidsfönstren överlappar, annars False
        """
        ...

    def __str__(self) -> str:
        """Formaterar tidsfönstret som sträng.

        Returns:
            Tidsfönstret i formatet HH:MM-HH:MM, till exempel "10:00-11:30".
        """
        ...


@dataclass
class Event:
    """En kalenderhändelse.

    En händelse går att ändra: den kan till exempel avbokas.

    Attribut:
        event_id: Unik identifierare för händelsen
        title: Titel/beskrivning av händelsen
        slot: Händelsens tidsfönster
        participants: Lista med deltagare (användarnamn/ID)
        cancelled: Om händelsen är avbokad
    """

    def duration_minutes(self) -> int:
        """Beräknar händelsens längd i minuter.

        Returns:
            Antal minuter i händelsens tidsfönster
        """
        ...

    def has_participant(self, participant: Participant) -> bool:
        """Kontrollerar om en viss person deltar.

        Args:
            participant: Deltagarens namn/ID

        Returns:
            True om personen deltar
        """
        ...
