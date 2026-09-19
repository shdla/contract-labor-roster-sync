import hashlib
import hmac
import json
from datetime import date

import pytest

from roster_sync.events import (
    DeliveryError, InMemoryEventSender, WebhookEventSender, emit_diff, event_id,
    events_for_diff, worker_joined_event,
)
from roster_sync.models import RosterDiff, Worker
from roster_sync.normalize import normalize_name

D1, D2 = date(2024, 7, 15), date(2024, 7, 16)


def worker(first="Tomas", last="Ruiz", phones=("8325550214",), emails=("tr@example.com",), worker_id="w-1"):
    return Worker(worker_id=worker_id, name=normalize_name(first, last),
                  phones=set(phones), emails=set(emails), role="material_handler")


# -- event id and payload ---------------------------------------------------


def test_event_id_is_deterministic_across_calls():
    assert event_id("worker.joined", "w-1", D1) == event_id("worker.joined", "w-1", D1)


def test_event_id_differs_by_type_subject_or_period():
    base = event_id("worker.joined", "w-1", D1)
    assert event_id("worker.left", "w-1", D1) != base
    assert event_id("worker.joined", "w-2", D1) != base
    assert event_id("worker.joined", "w-1", D2) != base


def test_worker_joined_event_payload_shape():
    event = worker_joined_event(worker(), D1)
    assert event.payload == {
        "name": "Tomas Ruiz",
        "role": "material_handler",
        "phones": ["8325550214"],
        "emails": ["tr@example.com"],
    }
    assert event.id == event_id("worker.joined", "w-1", D1)


def test_body_is_an_envelope_with_routing_fields_at_the_top_level():
    event = worker_joined_event(worker(), D1)
    body = json.loads(event.body())
    assert body == {
        "event_id": event.id,
        "type": "worker.joined",
        "subject": "w-1",
        "occurred_on": "2024-07-15",
        "data": event.payload,
    }


def test_worker_with_no_email_still_carries_an_empty_array():
    event = worker_joined_event(worker(emails=()), D1)
    assert event.payload["emails"] == []


def test_events_for_diff_only_emits_joiners():
    joiner, leaver = worker("Tomas", "Ruiz"), worker("Alicia", "Fontenot")
    diff = RosterDiff(as_of=D1, joiners=[joiner], leavers=[leaver])
    events = events_for_diff(diff)
    assert [e.subject for e in events] == [joiner.worker_id]
    assert events[0].type == "worker.joined"


# -- in-memory sender ---------------------------------------------------


def test_in_memory_sender_records_sent_events_and_dedup_ids():
    sender = InMemoryEventSender()
    event = worker_joined_event(worker(), D1)
    sender.send(event)
    assert sender.sent == [event]
    assert sender.dedup_ids_seen == {event.id}


def test_emit_diff_reports_sent_ids():
    diff = RosterDiff(as_of=D1, joiners=[worker()])
    sender = InMemoryEventSender()
    outcome = emit_diff(diff, sender)
    assert outcome.summary() == {"sent": 1, "failed": 0}
    assert outcome.sent == [event_id("worker.joined", "w-1", D1)]


def test_emit_diff_failure_on_one_event_does_not_stop_the_rest():
    diff = RosterDiff(as_of=D1, joiners=[
        worker("Tomas", "Ruiz", worker_id="w-1"),
        worker("Alicia", "Fontenot", ("8325550288",), ("af@example.com",), worker_id="w-2"),
    ])

    class Flaky(InMemoryEventSender):
        def send(self, event):
            if event.subject == "w-1":
                raise DeliveryError("boom")
            super().send(event)

    outcome = emit_diff(diff, Flaky())
    assert outcome.summary() == {"sent": 1, "failed": 1}
    assert outcome.failed[0][0] == event_id("worker.joined", "w-1", D1)


# -- webhook sender -----------------------------------------------------


class FakeResponse:
    def __init__(self, status):
        self.status_code = status


class FakeSession:
    def __init__(self, script):
        self.script, self.requests = list(script), []

    def post(self, url, **kw):
        self.requests.append((url, kw))
        return self.script.pop(0)


def test_webhook_sender_signs_body_and_sets_dedup_header():
    session = FakeSession([FakeResponse(200)])
    sender = WebhookEventSender("https://hooks.example/worker_joined", "s3cret", session)
    event = worker_joined_event(worker(), D1)

    sender.send(event)

    url, kwargs = session.requests[0]
    assert url == "https://hooks.example/worker_joined"
    assert kwargs["data"] == event.body()
    assert kwargs["headers"]["X-Dedup-Id"] == event.id
    # Recomputed over the bytes actually posted, with the shared secret.
    expected = hmac.new(b"s3cret", kwargs["data"], hashlib.sha256).hexdigest()
    assert kwargs["headers"]["X-Signature-256"] == expected


def test_webhook_sender_same_event_twice_produces_same_dedup_header():
    session = FakeSession([FakeResponse(200), FakeResponse(200)])
    sender = WebhookEventSender("https://hooks.example/worker_joined", "s3cret", session)
    event = worker_joined_event(worker(), D1)

    sender.send(event)
    sender.send(event)

    first_headers = session.requests[0][1]["headers"]
    second_headers = session.requests[1][1]["headers"]
    assert first_headers["X-Dedup-Id"] == second_headers["X-Dedup-Id"]


def test_webhook_sender_retries_on_503_then_succeeds():
    session = FakeSession([FakeResponse(503), FakeResponse(200)])
    slept = []
    sender = WebhookEventSender("https://hooks.example/worker_joined", "s3cret", session,
                                backoff_seconds=0.1, sleep=slept.append)
    sender.send(worker_joined_event(worker(), D1))
    assert slept == [0.1]
    assert len(session.requests) == 2


def test_webhook_sender_gives_up_after_max_attempts():
    session = FakeSession([FakeResponse(503)] * 4)
    sender = WebhookEventSender("https://hooks.example/worker_joined", "s3cret", session,
                                max_attempts=4, sleep=lambda _: None)
    with pytest.raises(DeliveryError):
        sender.send(worker_joined_event(worker(), D1))


def test_webhook_sender_raises_on_non_retryable_failure():
    session = FakeSession([FakeResponse(400)])
    sender = WebhookEventSender("https://hooks.example/worker_joined", "s3cret", session)
    with pytest.raises(DeliveryError):
        sender.send(worker_joined_event(worker(), D1))
