"""Tester för datamodeller."""

from datetime import datetime

import pytest

from models import Event, TimeSlot


def test_event_creation() -> None:
    """Test: Skapa en giltig händelse."""
    event = Event(
        event_id="evt1",
        title="Möte",
        slot=TimeSlot(datetime(2024, 3, 15, 10, 0), datetime(2024, 3, 15, 11, 0)),
        participants=["sara"],
    )
    assert event.event_id == "evt1"
    assert event.title == "Möte"
    assert len(event.participants) == 1
    assert not event.cancelled


def test_timeslot_invalid_times() -> None:
    """Test: Sluttid före starttid ska ge fel."""
    with pytest.raises(ValueError):
        TimeSlot(datetime(2024, 3, 15, 11, 0), datetime(2024, 3, 15, 10, 0))


def test_timeslot_overlaps_yes() -> None:
    """Test: Två tidsfönster som överlappar."""
    a = TimeSlot(datetime(2024, 3, 15, 10, 0), datetime(2024, 3, 15, 11, 0))
    b = TimeSlot(datetime(2024, 3, 15, 10, 30), datetime(2024, 3, 15, 11, 30))
    assert a.overlaps(b)
    assert b.overlaps(a)  # Symmetrisk
