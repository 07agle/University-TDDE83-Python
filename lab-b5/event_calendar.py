"""Huvudlogik för kalendersystemet.

Den här modulen innehåller klassen Calendar, som håller ordning på
händelserna och sparar dem i en lagring.
"""

from datetime import date

from models import Event, EventId
from storage import CalendarStorage


class Calendar:
    """Kalendersystem med händelsehantering.

    Använder en lagring som följer CalendarStorage-protokollet.
    """

    def __init__(self, storage: CalendarStorage) -> None:
        """Initierar kalendern med en given lagring.

        Args:
            storage: Lagringen för händelserna
        """
        ...

    def add_event(self, event: Event) -> None:
        """Lägger till en händelse i kalendern.

        Metoden ska spara händelsen om den är giltig i betydelsen att
        själva `Event`-objektet redan har kunnat skapas. Den ska inte
        stoppa tidskonflikter vid inläggning; sådana upptäcks separat med
        `has_conflicts()` eller `find_conflicts()`. Ogiltiga tider
        (t.ex. sluttid före starttid) stoppas redan när `TimeSlot` skapas.
        Samma förutsättning som i `CalendarStorage.add_event()` gäller:
        ingen händelse med samma event_id får redan finnas i kalendern.

        Args:
            event: Händelsen att lägga till
        """
        ...

    def get_event(self, event_id: EventId) -> Event | None:
        """Hämtar en händelse baserat på ID.

        Args:
            event_id: ID för händelsen

        Returns:
            Event-objektet eller None om inte hittat
        """
        ...

    def cancel_event(self, event_id: EventId) -> bool:
        """Avbokar en händelse genom att sätta dess fält cancelled till True.

        En händelse som redan är avbokad kan avbokas igen. Den förblir
        avbokad, och metoden returnerar True eftersom händelsen finns.

        Args:
            event_id: ID för händelsen att avboka

        Returns:
            True om händelsen fanns (och nu är avbokad), False om den inte fanns
        """
        ...

    def get_events_on_date(self, target_date: date) -> list[Event]:
        """Hämtar alla händelser på ett visst datum.

        Avbokade händelser räknas med; det syns på deras fält cancelled.

        Args:
            target_date: Datumet att söka efter

        Returns:
            Lista med händelser vars tidsfönster startar på det datumet
        """
        ...

    def find_conflicts(self) -> list[tuple[EventId, EventId]]:
        """Hittar alla par av händelser som är i konflikt.

        Två händelser är i konflikt om ingen av dem är avbokad och deras
        tidsfönster överlappar. Varje par i konflikt finns med exakt en gång.

        Returns:
            Lista med tuple-par (event_id1, event_id2) för händelser i konflikt

        Examples:
            >>> from datetime import datetime
            >>> from models import Event, TimeSlot
            >>> from storage import ListStorage
            >>> cal = Calendar(ListStorage())
            >>> slot1 = TimeSlot(datetime(2024,3,15,10,0), datetime(2024,3,15,11,0))
            >>> slot2 = TimeSlot(datetime(2024,3,15,10,30), datetime(2024,3,15,11,30))
            >>> cal.add_event(Event("1", "Mtg1", slot1))
            >>> cal.add_event(Event("2", "Mtg2", slot2))
            >>> [tuple(sorted(pair)) for pair in cal.find_conflicts()]
            [('1', '2')]
        """
        ...

    def has_conflicts(self, event: Event) -> bool:
        """Kontrollerar om en given händelse är i konflikt med befintliga händelser.

        Händelsen själv (samma event_id) räknas inte som en konflikt, så en
        händelse som redan ligger i kalendern krockar inte med sig själv.
        Avbokade händelser krockar aldrig, varken den givna händelsen eller
        de som ligger i kalendern.

        Args:
            event: Händelsen att kontrollera

        Returns:
            True om händelsen är i konflikt med minst en annan befintlig händelse
        """
        ...
