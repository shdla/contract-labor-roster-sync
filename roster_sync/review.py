"""Resolution of flagged rows, so a decision only has to be made once.

A queue that cannot be cleared is an alert, and people stop reading alerts.
The point of this module is that answering a flag changes the data, not just
a status column: confirming a match attaches the new phone or email to the
existing worker permanently, so next week that row matches on a strong
signal and never reaches the queue again.

Two outcomes:

- confirm  the row belongs to an existing worker; identifiers are merged in
- reject   the row is a different person; a worker is created deliberately

Neither outcome may give an identifier a second owner. A flag whose phone or
email another worker already holds can be confirmed only onto that holder and
cannot be rejected; one whose phone and email have different holders stays
open. Moving an identifier between workers is never done implicitly here.

Both are recorded with who decided and when, because deactivating somebody's
site access on the strength of a judgment call is the kind of thing that
gets asked about later.
"""

from __future__ import annotations

from dataclasses import dataclass

from .identity import WorkerRegistry
from .models import MatchConfidence, MatchResult, Worker
from .store import PendingReview, Store


@dataclass
class Resolution:
    """What a decision did, returned to the caller.

    After a reject the caller emits worker_joined_event(resolution.worker,
    review.as_of): the worker is created here, never in compute_diff's NEW
    branch, so no diff of that run lists the joiner. first_seen is
    review.as_of, so a rerun of that period derives the same joiner and the
    same event id, and the receiver deduplicates.
    """

    decision: str
    worker: Worker
    changes: list[str]


class ReviewResolutionError(RuntimeError):
    pass


def confirm(
    store: Store,
    registry: WorkerRegistry,
    review_id: str,
    worker_id: str,
    decided_by: str,
) -> Resolution:
    """Attach a flagged row to an existing worker and persist the decision."""
    review = _load_open(store, review_id)
    worker = registry.get(worker_id)
    if worker is None:
        raise ReviewResolutionError(f"no worker {worker_id}")

    conflict = _identifier_owner(registry, review, exclude=worker_id)
    if conflict is not None:
        raise ReviewResolutionError(
            f"{_held_identifier(review, conflict)} already belongs to "
            f"{conflict.name.display}; resolve that worker first"
        )

    result = MatchResult(row=review.row, confidence=MatchConfidence.WEAK_NAME, worker=worker)
    changes = registry.apply(result, review.as_of)

    store.save_registry(registry)
    store.record_decision(review_id, "confirmed", worker.worker_id, decided_by)
    return Resolution("confirmed", worker, changes)


def reject(
    store: Store,
    registry: WorkerRegistry,
    review_id: str,
    decided_by: str,
) -> Resolution:
    """Treat a flagged row as a distinct person and create the worker."""
    review = _load_open(store, review_id)

    conflict = _identifier_owner(registry, review, exclude=None)
    if conflict is not None:
        raise ReviewResolutionError(
            f"{_held_identifier(review, conflict)} already belongs to "
            f"{conflict.name.display}; this row cannot be a new person"
        )

    worker = registry.create(review.row, review.as_of)
    store.save_registry(registry)
    store.record_decision(review_id, "rejected", worker.worker_id, decided_by)
    return Resolution("rejected", worker, [])


def _load_open(store: Store, review_id: str) -> PendingReview:
    """Load a flag that is still undecided. A second decision would overwrite who made the first."""
    review = store.get_review(review_id)
    if review is None:
        raise ReviewResolutionError(f"no review {review_id}")
    if review.decision is not None:
        raise ReviewResolutionError(f"review {review_id} already {review.decision}")
    return review


def _identifier_owner(
    registry: WorkerRegistry, review: PendingReview, exclude: str | None
) -> Worker | None:
    """Find a worker other than `exclude` already holding this row's phone or email.

    Asks the identifier indexes, not match(): a flagged row whose identifier
    belongs to a differently named worker probes as CONFLICT, never STRONG.
    """
    return next((w for w in registry.owners_of(review.row) if w.worker_id != exclude), None)


def _held_identifier(review: PendingReview, owner: Worker) -> str | None:
    """The identifier on the row that `owner` holds, so the refusal names the right one."""
    return review.row.phone if review.row.phone in owner.phones else review.row.email
