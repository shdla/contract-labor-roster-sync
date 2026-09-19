import hashlib
import hmac
import json
from datetime import date

import pytest

from roster_sync.diff import compute_diff
from roster_sync.events import (
    DeliveryError, InMemoryEventSender, WebhookEventSender, emit_diff, event_id,
    events_for_diff, worker_joined_event,
)
from roster_sync.identity import WorkerRegistry
from roster_sync.models import RosterDiff, RosterRow, Worker
from roster_sync.normalize import normalize_name, normalize_phone
from roster_sync.review import reject
from roster_sync.store import Store

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


def test_event_id_is_pinned_to_a_golden_value():
    # The receiver deduplicates on this id. Changing EVENT_ID_NAMESPACE or the
    # name format gives every in-flight event a new id, and a re-sent event
    # would then be processed a second time.
    assert event_id("worker.joined", "w-1", date(2024, 7, 15)) == "36d4c58c-252a-5a6d-81ea-ac1d2e71c893"


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


# -- reruns ------------------------------------------------------------------


def roster_row(first, last, phone, source_row=2):
    return RosterRow(source_row=source_row, name=normalize_name(first, last),
                     phone=normalize_phone(phone), email=None, role="material_handler")


def test_rerun_of_a_period_re_emits_the_same_event_ids():
    registry = WorkerRegistry()
    rows = [roster_row("Tomas", "Ruiz", "8325550214"),
            roster_row("Alicia", "Fontenot", "8325550288", source_row=3)]

    first = compute_diff(registry, rows, D1)
    rerun = compute_diff(registry, rows, D1)

    # EmitOutcome.failed is persisted nowhere, so a rerun is the only resend
    # path, and the receiver can only deduplicate it if the ids repeat.
    assert rerun.is_rerun is True
    sent = [e.id for e in events_for_diff(first)]
    assert len(sent) == 2
    assert [e.id for e in events_for_diff(rerun)] == sent


def test_worker_created_by_reject_gets_the_event_id_a_rerun_of_that_period_derives(tmp_path):
    path = tmp_path / "roster.db"
    other = roster_row("Chris", "Nguyen", "8325550202")

    with Store(path) as store:
        registry = store.load_registry()
        compute_diff(registry, [roster_row("Chris", "Nguyen", "8325550101")], D1)

        flagged = compute_diff(registry, [other], D2)
        assert events_for_diff(flagged) == [], "a flagged row is nobody's joiner yet"
        (review_id,) = store.save_reviews(flagged.review, D2)

        # The caller's emission after a reject, as the Resolution docstring states it.
        resolution = reject(store, registry, review_id, decided_by="a.diaz")
        emitted = worker_joined_event(resolution.worker, store.get_review(review_id).as_of)

    # The period is run again in a fresh process; first_seen comes back from the database.
    with Store(path) as later:
        rerun = compute_diff(later.load_registry(), [other], D2)
        assert [e.id for e in events_for_diff(rerun)] == [emitted.id]


# -- in-memory sender ---------------------------------------------------


def test_in_memory_sender_records_sent_events():
    sender = InMemoryEventSender()
    event = worker_joined_event(worker(), D1)
    sender.send(event)
    assert sender.sent == [event]


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


def test_webhook_sender_retries_on_429_and_503_then_succeeds():
    session = FakeSession([FakeResponse(429), FakeResponse(503), FakeResponse(200)])
    slept = []
    sender = WebhookEventSender("https://hooks.example/worker_joined", "s3cret", session,
                                backoff_seconds=0.1, sleep=slept.append)
    sender.send(worker_joined_event(worker(), D1))
    assert slept == [0.1, 0.2]
    assert len(session.requests) == 3


def test_webhook_sender_gives_up_after_max_attempts():
    session = FakeSession([FakeResponse(503)] * 4)
    slept = []
    sender = WebhookEventSender("https://hooks.example/worker_joined", "s3cret", session,
                                max_attempts=4, backoff_seconds=0.5, sleep=slept.append)
    with pytest.raises(DeliveryError, match=r"after 4 attempt\(s\): status 503"):
        sender.send(worker_joined_event(worker(), D1))
    assert len(session.requests) == 4
    assert slept == [0.5, 1.0, 2.0], "no sleep after the last attempt"


def test_webhook_sender_raises_on_non_retryable_failure():
    session = FakeSession([FakeResponse(400)])
    sender = WebhookEventSender("https://hooks.example/worker_joined", "s3cret", session)
    with pytest.raises(DeliveryError, match=r"after 1 attempt\(s\): status 400"):
        sender.send(worker_joined_event(worker(), D1))


def test_transport_error_on_one_event_is_retried_recorded_and_does_not_stop_the_rest():
    diff = RosterDiff(as_of=D1, joiners=[
        worker("Tomas", "Ruiz", worker_id="w-1"),
        worker("Alicia", "Fontenot", ("8325550288",), ("af@example.com",), worker_id="w-2"),
    ])
    first = event_id("worker.joined", "w-1", D1)

    class TimesOutForTheFirstEvent(FakeSession):
        def post(self, url, **kw):
            if kw["headers"]["X-Dedup-Id"] == first:
                self.requests.append((url, kw))
                raise TimeoutError("read timed out")
            return super().post(url, **kw)

    session = TimesOutForTheFirstEvent([FakeResponse(200)])
    slept = []
    sender = WebhookEventSender("https://hooks.example/worker_joined", "s3cret", session,
                                max_attempts=4, backoff_seconds=0.5, sleep=slept.append)
    outcome = emit_diff(diff, sender)

    assert outcome.summary() == {"sent": 1, "failed": 1}
    assert outcome.failed[0][0] == first
    assert "after 4 attempt(s): TimeoutError" in outcome.failed[0][1]
    assert len(session.requests) == 5, "four tries for the first event, then the second is still sent"
    assert slept == [0.5, 1.0, 2.0], "a timeout backs off like a 503"
