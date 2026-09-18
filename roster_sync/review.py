"""Resolution of flagged rows, so a decision only has to be made once.

A queue that cannot be cleared is an alert, and people stop reading alerts.
The point of this module is that answering a flag changes the data, not just
a status column: confirming a match attaches the new phone or email to the
existing worker permanently, so next week that row matches on a strong
signal and never reaches the queue again.

Two outcomes:

- confirm  the row belongs to an existing worker; identifiers are merged in
- reject   the row is a different person; a worker is created deliberately

Both are recorded with who decided and when, because deactivating somebody's
site access on the strength of a judgement call is the kind of thing that
gets asked about later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .identity import WorkerRegistry
from .models import MatchConfidence, MatchResult, Worker
from .store import PendingReview, Store


@dataclass
class Resolution:
    review_id: str
    decision: str
    worker: Worker
    changes: list[str]
    created_worker: bool


class ReviewResolutionError(RuntimeError):
    pass


def confirm(
    store: Store,
    registry: WorkerRegistry,
    review_id: str,
    worker_id: str,
    decided_by: str,
    seen_on: date | None = None,
) -> Resolution:
    """Attach a flagged row to an existing worker and persist the decision."""
    review = _load_open(store, review_id)
    worker = registry.get(worker_id)
    if worker is None:
        raise ReviewResolutionError(f"no worker {worker_id}")

    conflict = _identifier_owner(registry, review, exclude=worker_id)
    if conflict is not None:
        raise ReviewResolutionError(
            f"{review.row.phone or review.row.email} already belongs to "
            f"{conflict.name.display}; resolve that worker first"
        )

    result = MatchResult(row=review.row, confidence=MatchConfidence.WEAK_NAME, worker=worker)
    changes = registry.apply(result, seen_on or review.as_of)

    store.save_registry(registry)
    store.record_decision(review_id, "confirmed", worker.worker_id, decided_by)
    return Resolution(review_id, "confirmed", worker, changes, created_worker=False)


def reject(
    store: Store,
    registry: WorkerRegistry,
    review_id: str,
    decided_by: str,
    seen_on: date | None = None,
) -> Resolution:
    """Treat a flagged row as a distinct person and create the worker."""
    review = _load_open(store, review_id)

    conflict = _identifier_owner(registry, review, exclude=None)
    if conflict is not None:
        raise ReviewResolutionError(
            f"{review.row.phone or review.row.email} already belongs to "
            f"{conflict.name.display}; this row cannot be a new person"
        )

    worker = registry.create(review.row, seen_on or review.as_of)
    store.save_registry(registry)
    store.record_decision(review_id, "rejected", worker.worker_id, decided_by)
    return Resolution(review_id, "rejected", worker, [], created_worker=True)


def _load_open(store: Store, review_id: str) -> PendingReview:
    review = store.get_review(review_id)
    if review is None:
        raise ReviewResolutionError(f"no review {review_id}")
    return review


def _identifier_owner(
    registry: WorkerRegistry, review: PendingReview, exclude: str | None
) -> Worker | None:
    """Find a worker already holding this row's phone or email."""
    probe = registry.match(review.row)
    if probe.worker is None:
        return None
    if probe.confidence not in (MatchConfidence.STRONG_PHONE, MatchConfidence.STRONG_EMAIL):
        return None
    if exclude is not None and probe.worker.worker_id == exclude:
        return None
    return probe.worker
