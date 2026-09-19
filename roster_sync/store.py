"""SQLite persistence for everything that must survive between runs: workers
and their identifiers, roster periods, the review queue, credentials, access
state and the badge map.

State has to survive between runs for three reasons. The registry is only
useful if this week's file can be compared against last week's population.
Absence is derived from the set of roster periods already processed, so that
set must persist. And a review flag a human has resolved must stay resolved,
or the same question returns every week until people stop reading the queue.
Credentials, pushed access state and badge ids persist for the same reason,
because the gate and idempotent provisioning read them.

Storage is deliberately kept behind this module: identity.py and diff.py
know nothing about it, so the matching logic stays testable in memory.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .credentials import Credential
from .identity import WorkerRegistry
from .models import MatchConfidence, MatchResult, RosterRow, Worker
from .normalize import NormalizedName

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    worker_id   TEXT PRIMARY KEY,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    role        TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    active      INTEGER NOT NULL DEFAULT 1
);

-- One row per identifier ever observed for a worker. The primary key on
-- (kind, value) is what enforces that a phone or email can only point at one
-- worker; an attempt to attach a known identifier to a second worker fails
-- loudly instead of silently splitting a person in two.
CREATE TABLE IF NOT EXISTS worker_identifiers (
    kind      TEXT NOT NULL,
    value     TEXT NOT NULL,
    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
    PRIMARY KEY (kind, value)
);

CREATE TABLE IF NOT EXISTS roster_periods (
    as_of        TEXT PRIMARY KEY,
    file_hash    TEXT,
    source_path  TEXT,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_reviews (
    review_id   TEXT PRIMARY KEY,
    as_of       TEXT NOT NULL,
    source_row  INTEGER,
    first_name  TEXT,
    last_name   TEXT,
    phone       TEXT,
    email       TEXT,
    role        TEXT,
    confidence  TEXT NOT NULL,
    note        TEXT,
    candidates  TEXT,
    created_at  TEXT NOT NULL,
    decision    TEXT,
    decided_worker_id TEXT,
    decided_by  TEXT,
    decided_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_reviews_open
    ON pending_reviews (decision) WHERE decision IS NULL;

-- Credential records are append-only. A renewal is a new row, never an
-- update to the old one, so the history of what somebody held and when
-- survives. The eligibility gate picks the latest expiry per kind.
CREATE TABLE IF NOT EXISTS credentials (
    credential_id TEXT PRIMARY KEY,
    worker_id     TEXT NOT NULL REFERENCES workers(worker_id),
    kind          TEXT NOT NULL,
    granted_on    TEXT NOT NULL,
    expires_on    TEXT,
    source        TEXT,
    reference     TEXT,
    recorded_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credentials_worker ON credentials (worker_id);

-- Last state pushed to the access system per worker. This is what makes
-- provisioning idempotent: a run against an unchanged population reads
-- these rows and makes no calls.
CREATE TABLE IF NOT EXISTS access_state (
    worker_id    TEXT PRIMARY KEY REFERENCES workers(worker_id),
    state        TEXT NOT NULL,
    external_ref TEXT,
    updated_at   TEXT NOT NULL
);

-- Badge id issued by the site, captured at badging so the site feed can be
-- joined without a manual lookup.
CREATE TABLE IF NOT EXISTS badge_map (
    badge_id  TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
    issued_on TEXT
);
"""


def file_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_date(text: str | None) -> date | None:
    return date.fromisoformat(text) if text else None


@dataclass
class PendingReview:
    """A flagged row awaiting a human decision, rehydrated from storage."""

    review_id: str
    as_of: date
    row: RosterRow
    confidence: MatchConfidence
    note: str
    candidate_ids: list[str]

    def describe(self, registry: WorkerRegistry) -> str:
        name = self.row.name.display if self.row.name else "(unnamed)"
        contact = self.row.phone or self.row.email or "no contact"
        names = [
            registry.get(i).name.display
            for i in self.candidate_ids
            if registry.get(i) is not None
        ]
        against = f" against {', '.join(names)}" if names else ""
        # .value is explicit: an f-string renders a str-mixin enum member as "weak_name" on
        # Python 3.9 but as "MatchConfidence.WEAK_NAME" from 3.11.
        return f"[{self.confidence.value}] {name} / {contact}{against} — {self.note}"


class Store:
    """A SQLite-backed home for all persisted state."""

    def __init__(self, path: str | Path = "roster.db") -> None:
        self.path = str(path)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        with closing(self._connection.cursor()) as cursor:
            cursor.executescript(SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- registry ---------------------------------------------------------

    def load_registry(self) -> WorkerRegistry:
        registry = WorkerRegistry()

        identifiers: dict[str, dict[str, set[str]]] = {}
        for row in self._connection.execute("SELECT * FROM worker_identifiers"):
            bucket = identifiers.setdefault(row["worker_id"], {"phone": set(), "email": set()})
            bucket[row["kind"]].add(row["value"])

        for row in self._connection.execute("SELECT * FROM workers"):
            bucket = identifiers.get(row["worker_id"], {"phone": set(), "email": set()})
            worker = Worker(
                worker_id=row["worker_id"],
                name=NormalizedName(first=row["first_name"], last=row["last_name"]),
                phones=set(bucket["phone"]),
                emails=set(bucket["email"]),
                role=row["role"],
                first_seen=_as_date(row["first_seen"]),
                last_seen=_as_date(row["last_seen"]),
                active=bool(row["active"]),
            )
            registry.adopt(worker)

        for row in self._connection.execute("SELECT as_of FROM roster_periods"):
            registry.record_period(date.fromisoformat(row["as_of"]))

        return registry

    def save_registry(self, registry: WorkerRegistry) -> None:
        """Write the whole registry back. Idempotent by construction.

        An identifier already stored under another worker raises, and the
        transaction rolls the whole save back. This is the backstop: by then
        the in-memory registry is already wrong, so review.py refuses first.
        """
        with self._connection:
            for worker in registry.workers:
                self._connection.execute(
                    """
                    INSERT INTO workers
                        (worker_id, first_name, last_name, role, first_seen, last_seen, active)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        first_name = excluded.first_name,
                        last_name  = excluded.last_name,
                        role       = excluded.role,
                        first_seen = excluded.first_seen,
                        last_seen  = excluded.last_seen,
                        active     = excluded.active
                    """,
                    (
                        worker.worker_id,
                        worker.name.first,
                        worker.name.last,
                        worker.role,
                        worker.first_seen.isoformat() if worker.first_seen else None,
                        worker.last_seen.isoformat() if worker.last_seen else None,
                        int(worker.active),
                    ),
                )
                pairs = [("phone", v) for v in worker.phones] + [("email", v) for v in worker.emails]
                for kind, value in pairs:
                    held = self._connection.execute(
                        "SELECT worker_id FROM worker_identifiers WHERE kind = ? AND value = ?",
                        (kind, value),
                    ).fetchone()
                    if held is not None and held["worker_id"] != worker.worker_id:
                        raise sqlite3.IntegrityError(
                            f"{kind} {value} belongs to worker {held['worker_id']}; "
                            f"refusing to attach it to worker {worker.worker_id}"
                        )
                    self._connection.execute(
                        """
                        INSERT INTO worker_identifiers (kind, value, worker_id)
                        VALUES (?, ?, ?)
                        ON CONFLICT(kind, value) DO NOTHING
                        """,
                        (kind, value, worker.worker_id),
                    )

    # -- roster periods ---------------------------------------------------

    def record_period(self, as_of: date, source_path: str | None = None,
                      digest: str | None = None) -> bool:
        """Record a processed roster period. False if it was already recorded."""
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO roster_periods (as_of, file_hash, source_path, processed_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT(as_of) DO NOTHING",
                (as_of.isoformat(), digest, source_path, _now()),
            )
        return cursor.rowcount == 1

    def period_hash(self, as_of: date) -> str | None:
        row = self._connection.execute(
            "SELECT file_hash FROM roster_periods WHERE as_of = ?", (as_of.isoformat(),)
        ).fetchone()
        return row["file_hash"] if row else None

    # -- review queue -----------------------------------------------------

    def save_reviews(self, results: list[MatchResult], as_of: date) -> list[str]:
        """Persist flagged rows and return the ids created. Re-flagging the same row does not duplicate it."""
        created: list[str] = []
        with self._connection:
            for result in results:
                row = result.row
                fingerprint = "|".join(
                    [
                        as_of.isoformat(),
                        row.name.key if row.name else "",
                        row.phone or "",
                        row.email or "",
                    ]
                )
                review_id = str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint))
                cursor = self._connection.execute(
                    """
                    INSERT INTO pending_reviews
                        (review_id, as_of, source_row, first_name, last_name, phone,
                         email, role, confidence, note, candidates, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(review_id) DO NOTHING
                    """,
                    (
                        review_id,
                        as_of.isoformat(),
                        row.source_row,
                        row.name.first if row.name else None,
                        row.name.last if row.name else None,
                        row.phone,
                        row.email,
                        row.role,
                        result.confidence.value,
                        result.note,
                        ",".join(w.worker_id for w in result.candidates),
                        _now(),
                    ),
                )
                if cursor.rowcount == 1:
                    created.append(review_id)
        return created

    def open_reviews(self) -> list[PendingReview]:
        rows = self._connection.execute(
            "SELECT * FROM pending_reviews WHERE decision IS NULL ORDER BY created_at"
        ).fetchall()
        return [self._to_review(r) for r in rows]

    def get_review(self, review_id: str) -> PendingReview | None:
        row = self._connection.execute(
            "SELECT * FROM pending_reviews WHERE review_id = ?", (review_id,)
        ).fetchone()
        return self._to_review(row) if row else None

    def record_decision(self, review_id: str, decision: str,
                        worker_id: str | None, decided_by: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE pending_reviews
                   SET decision = ?, decided_worker_id = ?, decided_by = ?, decided_at = ?
                 WHERE review_id = ?
                """,
                (decision, worker_id, decided_by, _now(), review_id),
            )

    # -- credentials ------------------------------------------------------

    def grant_credential(self, credential: Credential) -> str:
        """Record a credential. Returns its id. Duplicates are not merged;
        two identical grants are two facts about the world."""
        if self._connection.execute(
            "SELECT 1 FROM workers WHERE worker_id = ?", (credential.worker_id,)
        ).fetchone() is None:
            raise ValueError(f"no worker {credential.worker_id}")
        credential_id = str(uuid.uuid4())
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO credentials
                    (credential_id, worker_id, kind, granted_on, expires_on, source, reference, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    credential.worker_id,
                    credential.kind,
                    credential.granted_on.isoformat(),
                    credential.expires_on.isoformat() if credential.expires_on else None,
                    credential.source,
                    credential.reference,
                    _now(),
                ),
            )
        return credential_id

    def credentials_for(self, worker_id: str) -> list[Credential]:
        rows = self._connection.execute(
            "SELECT * FROM credentials WHERE worker_id = ? ORDER BY granted_on", (worker_id,)
        ).fetchall()
        return [self._to_credential(r) for r in rows]

    def all_credentials(self) -> dict[str, list[Credential]]:
        grouped: dict[str, list[Credential]] = {}
        for row in self._connection.execute("SELECT * FROM credentials ORDER BY granted_on"):
            grouped.setdefault(row["worker_id"], []).append(self._to_credential(row))
        return grouped

    # -- access state & badges --------------------------------------------

    def access_state(self, worker_id: str) -> tuple[str | None, str | None]:
        row = self._connection.execute(
            "SELECT state, external_ref FROM access_state WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        return (row["state"], row["external_ref"]) if row else (None, None)

    def set_access_state(self, worker_id: str, state: str, external_ref: str | None) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO access_state (worker_id, state, external_ref, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    state = excluded.state, external_ref = excluded.external_ref,
                    updated_at = excluded.updated_at
                """,
                (worker_id, state, external_ref, _now()),
            )

    def map_badge(self, badge_id: str, worker_id: str, issued_on: date | None = None) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO badge_map (badge_id, worker_id, issued_on) VALUES (?, ?, ?)"
                " ON CONFLICT(badge_id) DO UPDATE SET worker_id = excluded.worker_id",
                (badge_id, worker_id, issued_on.isoformat() if issued_on else None),
            )

    def badge_map(self) -> dict[str, str]:
        return {r["badge_id"]: r["worker_id"] for r in self._connection.execute("SELECT * FROM badge_map")}

    @staticmethod
    def _to_credential(row: sqlite3.Row) -> Credential:
        return Credential(
            worker_id=row["worker_id"],
            kind=row["kind"],
            granted_on=date.fromisoformat(row["granted_on"]),
            expires_on=_as_date(row["expires_on"]),
            source=row["source"] or "",
            reference=row["reference"] or "",
        )

    @staticmethod
    def _to_review(row: sqlite3.Row) -> PendingReview:
        name = None
        if row["first_name"] and row["last_name"]:
            name = NormalizedName(first=row["first_name"], last=row["last_name"])
        return PendingReview(
            review_id=row["review_id"],
            as_of=date.fromisoformat(row["as_of"]),
            row=RosterRow(
                source_row=row["source_row"] or 0,
                name=name,
                phone=row["phone"],
                email=row["email"],
                role=row["role"],
            ),
            confidence=MatchConfidence(row["confidence"]),
            note=row["note"] or "",
            candidate_ids=[i for i in (row["candidates"] or "").split(",") if i],
        )
