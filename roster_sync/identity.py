"""Resolution of roster rows to workers when the source has no stable key.

The agency roster carries first name, last name, phone, email and role. None
of these is guaranteed stable or unique, so the registry issues its own
identifier on first sight and matches later appearances through a cascade of
progressively weaker signals.

Design rules:

1. Never derive the worker_id from source values. Names and phones change;
   an identifier derived from them changes with them and breaks idempotency.
2. Auto-merge only on strong, unique signals (phone or email).
3. A name-only match is a suggestion for a human, never an automatic merge.
4. When a strong signal points at one worker and the name points at another,
   that is a conflict and is escalated rather than resolved by precedence.
"""

from __future__ import annotations

import uuid
from datetime import date

from .models import MatchConfidence, MatchResult, RosterRow, Worker


class WorkerRegistry:
    """The known population, indexed for matching."""

    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}
        self._by_phone: dict[str, str] = {}
        self._by_email: dict[str, str] = {}
        self._by_name: dict[str, set[str]] = {}
        self._periods: set[date] = set()

    # -- roster periods ---------------------------------------------------

    @property
    def periods(self) -> list[date]:
        """Roster periods processed so far, oldest first."""
        return sorted(self._periods)

    def record_period(self, as_of: date) -> bool:
        """Register a roster period. Returns False if already processed.

        Absence is derived from this set rather than from a counter, so
        reprocessing the same file is a no-op instead of aging every worker
        who happened to be missing from it.
        """
        if as_of in self._periods:
            return False
        self._periods.add(as_of)
        return True

    def periods_missed(self, worker: Worker) -> int:
        """How many processed periods have elapsed since this worker appeared."""
        if worker.last_seen is None:
            return 0
        return sum(1 for period in self._periods if period > worker.last_seen)

    # -- read -------------------------------------------------------------

    @property
    def workers(self) -> list[Worker]:
        return list(self._workers.values())

    def get(self, worker_id: str) -> Worker | None:
        return self._workers.get(worker_id)

    def active_workers(self) -> list[Worker]:
        return [w for w in self._workers.values() if w.active]

    # -- matching ---------------------------------------------------------

    def match(self, row: RosterRow) -> MatchResult:
        """Resolve a row against the known population without mutating it."""
        if not row.is_usable:
            return MatchResult(row=row, confidence=MatchConfidence.CONFLICT,
                               note="row lacks a name or any contact identifier")

        phone_hit = self._by_phone.get(row.phone) if row.phone else None
        email_hit = self._by_email.get(row.email) if row.email else None
        name_hits = self._by_name.get(row.name.key, set()) if row.name else set()

        # Two strong signals disagreeing means the source data is wrong or a
        # phone has been reassigned. Either way a human decides.
        if phone_hit and email_hit and phone_hit != email_hit:
            return MatchResult(
                row=row,
                confidence=MatchConfidence.CONFLICT,
                candidates=[self._workers[phone_hit], self._workers[email_hit]],
                note="phone and email resolve to different workers",
            )

        strong_hit = phone_hit or email_hit
        if strong_hit:
            worker = self._workers[strong_hit]
            # A strong signal pointing at a different name is usually a
            # recycled phone number or a data entry error on the name.
            if row.name and worker.name != row.name and name_hits and strong_hit not in name_hits:
                return MatchResult(
                    row=row,
                    confidence=MatchConfidence.CONFLICT,
                    worker=worker,
                    candidates=[self._workers[i] for i in name_hits],
                    note=(
                        f"contact identifier matches {worker.name.display} "
                        f"but the name matches a different worker"
                    ),
                )
            confidence = MatchConfidence.STRONG_PHONE if phone_hit else MatchConfidence.STRONG_EMAIL
            return MatchResult(row=row, confidence=confidence, worker=worker)

        if len(name_hits) == 1:
            worker = self._workers[next(iter(name_hits))]
            return MatchResult(
                row=row,
                confidence=MatchConfidence.WEAK_NAME,
                worker=worker,
                candidates=[worker],
                note="name matches an existing worker but no contact identifier does",
            )

        if len(name_hits) > 1:
            return MatchResult(
                row=row,
                confidence=MatchConfidence.CONFLICT,
                candidates=[self._workers[i] for i in name_hits],
                note="name matches more than one existing worker",
            )

        return MatchResult(row=row, confidence=MatchConfidence.NEW)

    def owners_of(self, row: RosterRow) -> list[Worker]:
        """Workers holding this row's phone or email, phone first, each once.

        Ownership is a fact in the indexes. match() cannot answer it: it folds
        the name in, so an identifier held by a differently named worker comes
        back as CONFLICT rather than as an owner.
        """
        phone_hit = self._by_phone.get(row.phone) if row.phone else None
        email_hit = self._by_email.get(row.email) if row.email else None
        return [self._workers[i] for i in dict.fromkeys((phone_hit, email_hit)) if i]

    # -- write ------------------------------------------------------------

    def create(self, row: RosterRow, seen_on: date) -> Worker:
        worker = Worker(
            worker_id=str(uuid.uuid4()),
            name=row.name,
            role=row.role,
            first_seen=seen_on,
            last_seen=seen_on,
        )
        if row.phone:
            worker.phones.add(row.phone)
        if row.email:
            worker.emails.add(row.email)
        self._index(worker)
        self._workers[worker.worker_id] = worker
        return worker

    def adopt(self, worker: Worker) -> Worker:
        """Load an existing worker, identifier included, from storage."""
        self._workers[worker.worker_id] = worker
        self._index(worker)
        return worker

    def apply(self, result: MatchResult, seen_on: date) -> list[str]:
        """Record an observation against a matched worker and reindex it."""
        worker = result.worker
        if worker is None:
            raise ValueError("apply() requires a matched worker")
        self._deindex(worker)
        changes = worker.observe(result.row, seen_on)
        worker.active = True
        self._index(worker)
        return changes

    def release(self, worker: Worker, row: RosterRow) -> list[tuple[str, str]]:
        """Take the row's phone and email off a worker who holds them. Returns the (kind, value) pairs.

        The only way an identifier leaves a worker, and only review.confirm
        calls it, on a reviewer's explicit transfer. The set and the index
        change together, so the identifier has no owner until apply() gives
        it one, and never two.
        """
        released: list[tuple[str, str]] = []
        if row.phone in worker.phones:
            worker.phones.discard(row.phone)
            self._by_phone.pop(row.phone, None)
            released.append(("phone", row.phone))
        if row.email in worker.emails:
            worker.emails.discard(row.email)
            self._by_email.pop(row.email, None)
            released.append(("email", row.email))
        return released

    def deactivate(self, worker: Worker) -> None:
        worker.active = False

    # -- indexing ---------------------------------------------------------

    def _index(self, worker: Worker) -> None:
        for phone in worker.phones:
            self._by_phone[phone] = worker.worker_id
        for email in worker.emails:
            self._by_email[email] = worker.worker_id
        self._by_name.setdefault(worker.name.key, set()).add(worker.worker_id)

    def _deindex(self, worker: Worker) -> None:
        for phone in list(worker.phones):
            self._by_phone.pop(phone, None)
        for email in list(worker.emails):
            self._by_email.pop(email, None)
        bucket = self._by_name.get(worker.name.key)
        if bucket:
            bucket.discard(worker.worker_id)
            if not bucket:
                del self._by_name[worker.name.key]
