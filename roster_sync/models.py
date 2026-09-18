"""Data structures shared across ingest, identity resolution and diffing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .normalize import NormalizedName


@dataclass(frozen=True)
class RosterRow:
    """One normalized row from an agency roster file.

    source_row is retained so every exception report can point a human at the
    exact line in the spreadsheet that caused it.
    """

    source_row: int
    name: NormalizedName | None
    phone: str | None
    email: str | None
    role: str | None
    raw: dict[str, object] = field(default_factory=dict)

    @property
    def is_usable(self) -> bool:
        """A row needs a name and at least one contact identifier."""
        return self.name is not None and (self.phone is not None or self.email is not None)


class MatchConfidence(str, Enum):
    """How a roster row was matched to a known worker.

    STRONG matches are applied automatically. WEAK matches are surfaced for
    human confirmation and never merged on their own: merging two people is
    materially worse than carrying a duplicate for a day.
    """

    NEW = "new"
    STRONG_PHONE = "strong_phone"
    STRONG_EMAIL = "strong_email"
    WEAK_NAME = "weak_name"
    CONFLICT = "conflict"

    @property
    def is_automatic(self) -> bool:
        return self in (MatchConfidence.NEW, MatchConfidence.STRONG_PHONE, MatchConfidence.STRONG_EMAIL)


@dataclass
class Worker:
    """A person, tracked across roster files by a locally issued identifier.

    worker_id is generated once and never derived from name, phone or email,
    because all three change. Identifiers observed over time accumulate as
    aliases so a person remains matchable after a phone change.
    """

    worker_id: str
    name: NormalizedName
    phones: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    role: str | None = None
    first_seen: date | None = None
    last_seen: date | None = None
    active: bool = True

    def observe(self, row: RosterRow, seen_on: date) -> list[str]:
        """Record an appearance. Returns a list of human-readable changes."""
        changes: list[str] = []

        if row.phone and row.phone not in self.phones:
            changes.append(f"phone added {row.phone}")
            self.phones.add(row.phone)
        if row.email and row.email not in self.emails:
            changes.append(f"email added {row.email}")
            self.emails.add(row.email)
        if row.role and row.role != self.role:
            changes.append(f"role {self.role or 'unset'} -> {row.role}")
            self.role = row.role
        if row.name and row.name != self.name:
            changes.append(f"name {self.name.display} -> {row.name.display}")
            self.name = row.name

        if self.first_seen is None:
            self.first_seen = seen_on
        self.last_seen = seen_on
        return changes


@dataclass
class MatchResult:
    row: RosterRow
    confidence: MatchConfidence
    worker: Worker | None = None
    candidates: list[Worker] = field(default_factory=list)
    note: str = ""


@dataclass
class RosterDiff:
    """The output of comparing one roster file against known population."""

    as_of: date
    joiners: list[Worker] = field(default_factory=list)
    leavers: list[Worker] = field(default_factory=list)
    changed: list[tuple[Worker, list[str]]] = field(default_factory=list)
    unchanged: list[Worker] = field(default_factory=list)
    review: list[MatchResult] = field(default_factory=list)
    rejected: list[RosterRow] = field(default_factory=list)
    is_rerun: bool = False

    def summary(self) -> dict[str, int]:
        return {
            "joiners": len(self.joiners),
            "leavers": len(self.leavers),
            "changed": len(self.changed),
            "unchanged": len(self.unchanged),
            "review": len(self.review),
            "rejected": len(self.rejected),
        }
