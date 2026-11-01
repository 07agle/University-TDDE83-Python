"""Storage-abstraktioner för kalendersystemet.

Den här modulen definierar protokollet CalendarStorage och två
implementationer av det: ListStorage och DictStorage.

Filen är färdig. Ni ska inte ändra den, bara läsa den och använda
protokollet och lagringarna i event_calendar.py.
"""

from typing import Protocol

from models import Event, EventId


class CalendarStorage(Protocol):
    """Protokoll för att lagra kalenderhändelser.

    Detta är ett strukturellt protokoll (Protocol) som definierar vilket
    gränssnitt (interface) en lagringsklass måste ha. Alla klasser som har
    dessa metoder är automatiskt kompatibla.
    """

    def add_event(self, event: Event) -> None:
        """Lägger till en händelse i lagringen.

        Förutsätter att ingen händelse med samma event_id redan finns
        i lagringen; det är anroparens ansvar. Protokollet lovar ingenting
        om vad som händer om förutsättningen inte håller.

        Args:
            event: Händelsen att lägga till
        """
        ...

    def get_event(self, event_id: EventId) -> Event | None:
        """Hämtar en händelse baserat på ID.

        Händelsen som returneras är samma objekt som ligger i lagringen,
        ingen kopia.

        Args:
            event_id: ID för händelsen

        Returns:
            Event-objektet eller None om inte hittat
        """
        ...

    def get_all_events(self) -> list[Event]:
        """Hämtar alla händelser.

        Listan är ny för varje anrop, men händelserna i den är samma
        objekt som ligger i lagringen.

        Returns:
            Lista med alla händelser
        """
        ...

    def remove_event(self, event_id: EventId) -> bool:
        """Tar bort en händelse från lagringen.

        Args:
            event_id: ID för händelsen att ta bort

        Returns:
            True om händelsen togs bort, False om den inte fanns
        """
        ...


class ListStorage:
    """List-baserad implementation av CalendarStorage.

    Lagrar händelser i en lista. Enkel men inte optimal för stora dataset.
    """

    def __init__(self) -> None:
        """Initierar en tom lagring."""
        self._events: list[Event] = []

    def add_event(self, event: Event) -> None:
        """Lägger till händelse i listan.

        Förutsätter, som protokollet, att event_id inte redan finns.

        Args:
            event: Händelsen att lägga till
        """
        self._events.append(event)

    def get_event(self, event_id: EventId) -> Event | None:
        """Söker linjärt i listan efter händelse med givet ID.

        Args:
            event_id: ID för händelsen

        Returns:
            Event-objektet eller None om inte hittat
        """
        for event in self._events:
            if event.event_id == event_id:
                return event
        return None

    def get_all_events(self) -> list[Event]:
        """Returnerar kopia av listan.

        Returns:
            En ny lista med alla händelser
        """
        return list(self._events)

    def remove_event(self, event_id: EventId) -> bool:
        """Tar bort händelse från listan om den finns.

        Args:
            event_id: ID för händelsen att ta bort

        Returns:
            True om händelsen togs bort, False om den inte fanns
        """
        for i, event in enumerate(self._events):
            if event.event_id == event_id:
                self._events.pop(i)
                return True
        return False


class DictStorage:
    """Dictionary-baserad implementation av CalendarStorage.

    Lagrar händelser med ID som nyckel. Snabbare uppslag än ListStorage.
    """

    def __init__(self) -> None:
        """Initierar tom dictionary."""
        self._events: dict[EventId, Event] = {}

    def add_event(self, event: Event) -> None:
        """Lägger till händelse i dictionary.

        Förutsätter, som protokollet, att event_id inte redan finns.

        Args:
            event: Händelsen att lägga till
        """
        self._events[event.event_id] = event

    def get_event(self, event_id: EventId) -> Event | None:
        """Hämtar händelse direkt via dictionary-lookup.

        Args:
            event_id: ID för händelsen

        Returns:
            Event-objektet eller None om inte hittat
        """
        return self._events.get(event_id)

    def get_all_events(self) -> list[Event]:
        """Returnerar alla händelser som lista.

        Returns:
            En ny lista med alla händelser
        """
        return list(self._events.values())

    def remove_event(self, event_id: EventId) -> bool:
        """Tar bort händelse från dictionary om den finns.

        Args:
            event_id: ID för händelsen att ta bort

        Returns:
            True om händelsen togs bort, False om den inte fanns
        """
        if event_id in self._events:
            del self._events[event_id]
            return True
        return False
