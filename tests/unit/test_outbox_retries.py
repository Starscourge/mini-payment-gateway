import random
from datetime import UTC, datetime, timedelta

import pytest

from app.models import OutboxEvent, OutboxStatus
from app.services import outbox
from app.services.outbox import MAX_ATTEMPTS, record_failure, retry_delay


@pytest.fixture
def no_jitter(monkeypatch):
    monkeypatch.setattr(random, "uniform", lambda low, high: 1.0)


def test_delay_doubles_from_30_seconds_and_caps_at_6_hours(no_jitter):
    delays = [retry_delay(attempts).total_seconds() for attempts in range(1, 13)]
    assert delays == [30, 60, 120, 240, 480, 960, 1920, 3840, 7680, 15360, 21600, 21600]


@pytest.mark.parametrize("attempts", [1, 5, 12])
def test_jitter_stays_within_20_percent(attempts, monkeypatch):
    monkeypatch.setattr(random, "uniform", lambda low, high: 1.0)
    planned = retry_delay(attempts)
    monkeypatch.undo()
    for _ in range(200):
        assert planned * 0.8 <= retry_delay(attempts) <= planned * 1.2


def pending_event(attempts=0):
    return OutboxEvent(status=OutboxStatus.PENDING, attempts=attempts, payload={})


def test_failure_schedules_a_later_attempt(no_jitter):
    event = pending_event()
    before = datetime.now(UTC)
    record_failure(event, "bank returned HTTP 503")

    assert event.status == OutboxStatus.PENDING
    assert event.attempts == 1
    assert event.last_error == "bank returned HTTP 503"
    assert event.next_attempt_at - before >= timedelta(seconds=30)


def test_event_dies_on_the_tenth_failure():
    event = pending_event(attempts=MAX_ATTEMPTS - 2)
    record_failure(event, "still down")
    assert event.status == OutboxStatus.PENDING

    record_failure(event, "still down")
    assert event.status == OutboxStatus.DEAD
    assert event.attempts == MAX_ATTEMPTS


def test_long_errors_are_truncated():
    event = pending_event()
    record_failure(event, "x" * 10_000)
    assert event.last_error is not None
    assert len(event.last_error) == 500


def test_done_counts_the_attempt_and_clears_the_error():
    event = pending_event(attempts=2)
    event.last_error = "earlier failure"
    outbox.mark_done(event)
    assert (event.status, event.attempts, event.last_error) == (OutboxStatus.DONE, 3, None)
