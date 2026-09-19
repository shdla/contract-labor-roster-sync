"""Event emission to the iPaaS layer.

Workato owns everything that happens after a domain event fires:
orientation scheduling, notification, reminders, escalation. This module's
job stops at turning a RosterDiff into signed, deduplicable webhook
deliveries and getting them there — it says nothing about what happens once
they arrive.

Delivery is at-least-once (retries can double-send); a deterministic dedup
id is what makes that safe to replay rather than merely convenient. The id
is a UUIDv5 of (type, subject, period), so retrying the same
worker/period/event-type produces the same id and the receiver can discard
the repeat. Signing (HMAC-SHA256 over the raw JSON body) lets Workato's
webhook trigger — which needs no connection object — still verify the
sender.

Only worker.joined is emitted today, because the orientation-scheduling
recipe is the only consumer built so far. Leaver/changed events are not
invented here; add an event type only once a recipe exists to react to it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from .models import RosterDiff, Worker

log = logging.getLogger(__name__)

# Fixed namespace for UUIDv5 event ids -- arbitrary but permanent, so ids
# stay reproducible across processes and reruns. Not a secret.
EVENT_ID_NAMESPACE = uuid.UUID("a3f1e2d4-9b6c-4e10-8f2a-6d4c1b9e7f00")


def event_id(event_type: str, subject: str, period: date) -> str:
    """Deterministic id for (type, subject, period): same inputs, same id, across processes and reruns."""
    name = f"{event_type}:{subject}:{period.isoformat()}"
    return str(uuid.uuid5(EVENT_ID_NAMESPACE, name))


@dataclass(frozen=True)
class Event:
    type: str
    subject: str
    period: date
    payload: dict[str, object]

    @property
    def id(self) -> str:
        return event_id(self.type, self.subject, self.period)

    def envelope(self) -> dict[str, object]:
        # Routing fields sit at the top level so the receiver can guard on
        # type and dedup on event_id without opening data. The same id also
        # travels in the dedup header; the body copy is for job history.
        return {
            "event_id": self.id,
            "type": self.type,
            "subject": self.subject,
            "occurred_on": self.period.isoformat(),
            "data": self.payload,
        }

    def body(self) -> bytes:
        # sort_keys makes the signature reproducible regardless of the
        # payload dict's insertion order.
        return json.dumps(self.envelope(), sort_keys=True, separators=(",", ":")).encode("utf-8")


def worker_joined_event(worker: Worker, period: date) -> Event:
    payload = {
        "name": worker.name.display,
        "role": worker.role,
        "phones": sorted(worker.phones),
        "emails": sorted(worker.emails),
    }
    return Event(type="worker.joined", subject=worker.worker_id, period=period, payload=payload)


def events_for_diff(diff: RosterDiff) -> list[Event]:
    return [worker_joined_event(worker, diff.as_of) for worker in diff.joiners]


class DeliveryError(RuntimeError):
    pass


class EventSender(Protocol):
    def send(self, event: Event) -> None: ...


# -- in-memory ----------------------------------------------------------


class InMemoryEventSender:
    """Records deliveries instead of making them. Used in tests and demos."""

    def __init__(self) -> None:
        self.sent: list[Event] = []
        self.dedup_ids_seen: set[str] = set()

    def send(self, event: Event) -> None:
        self.sent.append(event)
        self.dedup_ids_seen.add(event.id)


# -- webhook --------------------------------------------------------------


class WebhookEventSender:
    """Delivers events to a webhook (Workato's Webhooks connector trigger).

    session is any object exposing .post returning something with
    .status_code; requests.Session satisfies this, and tests inject a fake --
    the same convention provisioning.py uses for the access API.
    """

    def __init__(self, url: str, signing_secret: str, session,
                 dedup_header: str = "X-Dedup-Id",
                 signature_header: str = "X-Signature-256",
                 max_attempts: int = 4, backoff_seconds: float = 0.5, sleep=time.sleep) -> None:
        self.url = url
        self.signing_secret = signing_secret.encode("utf-8")
        self.session = session
        self.dedup_header = dedup_header
        self.signature_header = signature_header
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep

    def signed_request(self, event: Event) -> tuple[bytes, dict[str, str]]:
        """Body and headers together, so the signature is over the exact bytes posted."""
        body = event.body()
        headers = {
            "Content-Type": "application/json",
            self.dedup_header: event.id,
            self.signature_header: hmac.new(self.signing_secret, body, hashlib.sha256).hexdigest(),
        }
        return body, headers

    def send(self, event: Event) -> None:
        body, headers = self.signed_request(event)
        last = None
        for attempt in range(1, self.max_attempts + 1):
            response = self.session.post(self.url, data=body, headers=headers, timeout=15)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < self.max_attempts:
                delay = self.backoff_seconds * (2 ** (attempt - 1))
                log.warning("webhook post -> %s; retry %d in %.1fs", response.status_code, attempt, delay)
                self._sleep(delay)
                last = response
                continue
            if response.status_code >= 300:
                raise DeliveryError(f"webhook post failed: {response.status_code}")
            return
        raise DeliveryError(f"webhook post failed after {self.max_attempts} attempts"
                            f" (last status {last.status_code if last else 'n/a'})")


# -- emit -------------------------------------------------------------------


@dataclass
class EmitOutcome:
    sent: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {"sent": len(self.sent), "failed": len(self.failed)}


def emit_diff(diff: RosterDiff, sender: EventSender) -> EmitOutcome:
    """Emit every event derived from a RosterDiff.

    A failure on one event is recorded and does not stop the rest -- same
    partial-progress rule provisioning.sync_access follows: one worker's
    delivery error should not block another's orientation notification.
    """
    outcome = EmitOutcome()
    for event in events_for_diff(diff):
        try:
            sender.send(event)
            outcome.sent.append(event.id)
        except DeliveryError as exc:
            log.error("event %s (%s) failed: %s", event.id, event.type, exc)
            outcome.failed.append((event.id, str(exc)))
    return outcome
