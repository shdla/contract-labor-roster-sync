"""Three-way hours reconciliation.

Three independent records exist for the same hours: what the agency reports
and invoices, what the contractor's own badge scanner captured and paid on,
and what the customer's site access system recorded as presence. Two-way
reconciliation shows that the numbers disagree. Three-way shows which one
is wrong:

    agency  scanner  site   reading
    ------  -------  ----   -------------------------------------------
      =        =      =     clean
      >        =      =     agency over-reported; dispute with evidence
      =        =      <     paid and present at the work area but not
                            badged at site; a gate-access question
      >        0      0     hours claimed that nobody recorded; dispute
      <        =      =     agency under-reported; correct the invoice
      =        =     n/a    clean on what we can see; site feed absent
      ?        ?      ?     incomplete punch; verify before disputing. A
                            source holds an in-punch with no out-punch, so
                            its hours are unknown, not zero; this reading
                            takes precedence over every row above

Sources arrive through adapters that all produce the same HoursRecord, so
the reconciliation never knows whether the site feed came from an SFTP
export, an API, or a manually uploaded file. That is the whole point of
the adapter: design for the access the customer will actually grant, and
swap the adapter when better access lands.

The agency file has the same missing-identifier problem as the roster. Its
rows are resolved through the same registry, so the worker id issued at
roster ingest is the join key across all three sources.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .identity import WorkerRegistry
from .models import MatchConfidence, RosterRow
from .normalize import normalize_email, normalize_name, normalize_phone

AGENCY, SCANNER, SITE = "agency", "scanner", "site"

# The one reading that is not a variance. Named because it is compared, not only printed.
CLEAN = "clean"


@dataclass(frozen=True)
class HoursRecord:
    """Hours attributed to one worker on one day from one source."""

    source: str
    worker_id: str
    day: date
    hours: float
    note: str = ""


@dataclass
class UnresolvedRow:
    source: str
    line: int
    reason: str
    raw: dict


# -- adapters -------------------------------------------------------------


def _parse_date(value: str) -> date:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date {value!r}")


def _parse_time(day: date, value: str) -> datetime:
    text = value.strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.combine(day, datetime.strptime(text, fmt).time())
        except ValueError:
            continue
    raise ValueError(f"unrecognized time {value!r}")


def read_agency_report(path: str | Path, registry: WorkerRegistry
                       ) -> tuple[list[HoursRecord], list[UnresolvedRow]]:
    """Agency pay-period report: name, phone/email, and total hours per day.

    Expected CSV columns (case-insensitive): first name, last name, phone,
    email, date, hours. Rows are resolved to workers through the registry;
    anything that does not match on a strong signal is returned unresolved
    rather than guessed, for the same reason the roster does it.
    """
    records: list[HoursRecord] = []
    unresolved: list[UnresolvedRow] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        reader.fieldnames = [f.strip().lower() for f in reader.fieldnames or []]
        for line, raw in enumerate(reader, start=2):
            # DictReader fills a short row with None; as empty cells it fails parsing below, not the whole file.
            raw = {k: v or "" for k, v in raw.items()}
            row = RosterRow(
                source_row=line,
                name=normalize_name(raw.get("first name"), raw.get("last name")),
                phone=normalize_phone(raw.get("phone")),
                email=normalize_email(raw.get("email")),
                role=None,
            )
            if not row.is_usable:
                unresolved.append(UnresolvedRow(AGENCY, line, "no usable identity", raw))
                continue
            match = registry.match(row)
            if match.confidence not in (MatchConfidence.STRONG_PHONE, MatchConfidence.STRONG_EMAIL):
                unresolved.append(UnresolvedRow(AGENCY, line, f"identity {match.confidence.value}", raw))
                continue
            try:
                records.append(HoursRecord(AGENCY, match.worker.worker_id,
                                           _parse_date(raw["date"]), float(raw["hours"])))
            except (KeyError, ValueError) as exc:
                unresolved.append(UnresolvedRow(AGENCY, line, str(exc), raw))
    return records, unresolved


def read_punch_log(path: str | Path, source: str, id_column: str = "worker_id"
                   ) -> tuple[list[HoursRecord], list[UnresolvedRow]]:
    """Punch events already keyed by worker id: worker_id, date, in, out.

    Used for the local scanner and, once the mapping table exists, for the
    site feed. A missing out-punch produces zero hours and a note rather
    than an assumption about when the person left; reconcile reads the note,
    so that zero is never classified as a variance.
    """
    records: list[HoursRecord] = []
    unresolved: list[UnresolvedRow] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        reader.fieldnames = [f.strip().lower() for f in reader.fieldnames or []]
        for line, raw in enumerate(reader, start=2):
            # Short rows arrive as None, read as empty cells; a row cut before "out" is a missing out-punch.
            raw = {k: v or "" for k, v in raw.items()}
            try:
                day = _parse_date(raw["date"])
                worker_id = raw[id_column].strip()
                if not raw.get("out", "").strip():
                    records.append(HoursRecord(source, worker_id, day, 0.0, "missing out-punch"))
                    continue
                start = _parse_time(day, raw["in"])
                end = _parse_time(day, raw["out"])
                if end < start:  # overnight shift rolls past midnight
                    end += timedelta(days=1)
                records.append(HoursRecord(source, worker_id, day, round((end - start).total_seconds() / 3600, 2)))
            except (KeyError, ValueError) as exc:
                unresolved.append(UnresolvedRow(source, line, str(exc), raw))
    return records, unresolved


def read_site_feed(path: str | Path, badge_to_worker: dict[str, str]
                   ) -> tuple[list[HoursRecord], list[UnresolvedRow]]:
    """Site badge export keyed by the customer's badge id, mapped to workers.

    badge_to_worker is the mapping captured at badging time. A badge id with
    no mapping is returned unresolved — that is exactly the lookup that used
    to be done by hand against a spreadsheet, and the fix is to capture the
    mapping when the badge is issued, not to reconstruct it per incident.
    """
    records, unresolved = read_punch_log(path, SITE, id_column="badge_id")
    resolved: list[HoursRecord] = []
    for record in records:
        worker_id = badge_to_worker.get(record.worker_id)
        if worker_id is None:
            unresolved.append(UnresolvedRow(SITE, 0, f"badge {record.worker_id} not mapped", {}))
            continue
        resolved.append(HoursRecord(SITE, worker_id, record.day, record.hours, record.note))
    return resolved, unresolved


# -- reconciliation -------------------------------------------------------


@dataclass
class DayVariance:
    day: date
    agency: float
    scanner: float
    site: float | None
    reading: str


@dataclass
class WorkerVariance:
    worker_id: str
    agency: float = 0.0
    scanner: float = 0.0
    site: float | None = None
    days: list[DayVariance] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return all(d.reading == CLEAN for d in self.days)


@dataclass
class ReconciliationReport:
    workers: list[WorkerVariance] = field(default_factory=list)
    unresolved: list[UnresolvedRow] = field(default_factory=list)

    @property
    def disputed(self) -> list[WorkerVariance]:
        return [w for w in self.workers if not w.clean]

    def summary(self) -> dict[str, float | int]:
        return {
            "workers": len(self.workers),
            "clean": sum(1 for w in self.workers if w.clean),
            "disputed": len(self.disputed),
            "agency_hours": round(sum(w.agency for w in self.workers), 2),
            "scanner_hours": round(sum(w.scanner for w in self.workers), 2),
            "over_reported_hours": round(sum(max(w.agency - w.scanner, 0) for w in self.workers), 2),
            "unresolved_rows": len(self.unresolved),
        }


def _classify(agency: float, scanner: float, site: float | None, tolerance: float) -> str:
    def near(a: float, b: float) -> bool:
        return abs(a - b) <= tolerance

    if site is None:
        if near(agency, scanner):
            return CLEAN
        return "agency over-reported" if agency > scanner else "agency under-reported"
    if agency > 0 and scanner == 0 and site == 0:
        return "claimed but unrecorded"
    if near(agency, scanner) and near(scanner, site):
        return CLEAN
    if agency > scanner and near(scanner, site):
        return "agency over-reported"
    if near(agency, scanner) and site + tolerance < scanner:
        return "present but not badged at site"
    if agency + tolerance < scanner and near(scanner, site):
        return "agency under-reported"
    return "mixed variance"


def reconcile(
    agency: list[HoursRecord],
    scanner: list[HoursRecord],
    site: list[HoursRecord] | None,
    period_start: date,
    period_end: date,
    tolerance_hours: float = 0.25,
    unresolved: Sequence[UnresolvedRow] = (),
) -> ReconciliationReport:
    """Compare sources per worker per day, then roll up to the pay period.

    Day-level detail is kept beneath the period totals so a variance can be
    named to a date rather than argued as a total.
    """
    by_key: dict[tuple[str, date], dict[str, float]] = defaultdict(lambda: {AGENCY: 0.0, SCANNER: 0.0, SITE: 0.0})
    for record in agency + scanner + (site or []):
        if record.worker_id and period_start <= record.day <= period_end:
            by_key[(record.worker_id, record.day)][record.source] += record.hours
    # A note marks an in-punch with no out-punch: proof of presence whose hours are unknown, not zero.
    incomplete = {(r.worker_id, r.day) for r in agency + scanner + (site or []) if r.note}

    per_worker: dict[str, WorkerVariance] = {}
    for (worker_id, day), hours in sorted(by_key.items()):
        entry = per_worker.setdefault(worker_id, WorkerVariance(worker_id=worker_id, site=0.0 if site is not None else None))
        site_hours = hours[SITE] if site is not None else None
        entry.agency += hours[AGENCY]
        entry.scanner += hours[SCANNER]
        if site is not None:
            entry.site = (entry.site or 0.0) + hours[SITE]
        if (worker_id, day) in incomplete:
            reading = "incomplete punch; verify before disputing"
        else:
            reading = _classify(hours[AGENCY], hours[SCANNER], site_hours, tolerance_hours)
        entry.days.append(DayVariance(day, hours[AGENCY], hours[SCANNER], site_hours, reading))

    return ReconciliationReport(workers=list(per_worker.values()), unresolved=list(unresolved))
