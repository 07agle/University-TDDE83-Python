"""Tester för kalenderlogik."""

from datetime import datetime

import pytest

from event_calendar import Calendar
from models import Event, TimeSlot
from storage import DictStorage, ListStorage


def make_slot(start_hour: int, end_hour: int) -> TimeSlot:
    """Skapar ett tidsfönster i hela timmar den 15 mars 2024.

    Args:
        start_hour: Timmen då tidsfönstret börjar
        end_hour: Timmen då tidsfönstret slutar

    Returns:
        Tidsfönstret från start_hour:00 till end_hour:00
    """
    return TimeSlot(
        datetime(2024, 3, 15, start_hour, 0), datetime(2024, 3, 15, end_hour, 0)
    )


@pytest.fixture
def calendars() -> list[Calendar]:
    """Skapar två tomma kalendrar, en för varje lagring.

    Returns:
        En kalender med ListStorage och en med DictStorage.
    """
    return [Calendar(ListStorage()), Calendar(DictStorage())]


def test_add_and_get_event(calendars: list[Calendar]) -> None:
    """Test: Lägg till två händelser och hämta var och en via sitt id."""
    for cal in calendars:
        event = Event("evt1", "Test", make_slot(10, 11))
        lunch = Event("evt2", "Lunch", make_slot(12, 13))

        cal.add_event(event)
        cal.add_event(lunch)
        retrieved = cal.get_event("evt1")

        assert retrieved is not None
        assert retrieved.title == "Test"
        assert cal.get_event("evt2") == lunch
