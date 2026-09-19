"""Comparison of one roster file against the known population.

The leaver rule deserves explanation. A worker absent from a single file is
not treated as departed, because one truncated export or one dropped row
would deactivate a badge for somebody still on site. Absence must persist
across a configurable number of roster periods before deactivation.
A joiner, by contrast, is acted on immediately: the cost of onboarding
somebody a day early is far lower than the cost of turning up unable to work.

Absence is derived from the set of roster periods processed rather than from
a counter incremented per run. That distinction matters operationally: a
counter ages every absent worker again each time the same file is
reprocessed, so a retried job could deactivate badges for people still on
site. Deriving it makes a rerun a no-op.

Joiner status is derived the same way: a worker a row resolves to, whose
first_seen is the period being processed, is a joiner of that period, on the
first run and on every rerun. A rerun is the only way to resend a
worker.joined event whose delivery failed, and it can only do that if it
lists the same joiners. A flagged row resolves to nobody, so a worker
created by a reject is not listed while the row stays flagged;
review.Resolution says who emits then.
"""

from __future__ import annotations

from datetime import date

from .identity import WorkerRegistry
from .models import MatchConfidence, MatchResult, RosterDiff, RosterRow


def compute_diff(
    registry: WorkerRegistry,
    rows: list[RosterRow],
    as_of: date,
    absence_threshold: int = 2,
) -> RosterDiff:
    """Apply a roster file to the registry and return what changed.

    absence_threshold is the number of roster periods a known worker must be
    missing from before being treated as a leaver.
    """
    diff = RosterDiff(as_of=as_of)
    diff.is_rerun = not registry.record_period(as_of)
    seen_ids: set[str] = set()

    for row in rows:
        if not row.is_usable:
            diff.rejected.append(row)
            continue

        result = registry.match(row)

        if result.confidence is MatchConfidence.NEW:
            worker = registry.create(row, as_of)
            seen_ids.add(worker.worker_id)
            diff.joiners.append(worker)
            continue

        if not result.confidence.is_automatic:
            # WEAK_NAME and CONFLICT both wait for a human. The row is not
            # applied, so nothing is provisioned on an uncertain identity.
            # Every worker it may belong to is still marked seen for this
            # period, so a flagged period never counts as missed in a later
            # run, answered or not. The protection ends when the row stops
            # appearing: an ignored queue cannot keep a departed worker's
            # badge active. seen_ids still guards this run, because on a
            # backfilled period the later ones already count as missed.
            if result.worker is not None:
                result.worker.mark_seen(as_of)
                seen_ids.add(result.worker.worker_id)
            for candidate in result.candidates:
                candidate.mark_seen(as_of)
                seen_ids.add(candidate.worker_id)
            diff.review.append(result)
            continue

        if result.worker.worker_id in seen_ids and row.name != result.worker.name:
            # A roster cannot list one person twice under two names, so the
            # second row is a second person on a shared identifier, and
            # applying it would rename the first. match() has already
            # escalated an incompatible name; what reaches here is a near
            # one ("Mario" after "Maria") that it read as a typo. The earlier
            # row already marked the worker seen, so nothing is marked here.
            diff.review.append(MatchResult(
                row=row,
                confidence=MatchConfidence.CONFLICT,
                worker=result.worker,
                candidates=[result.worker],
                note=(
                    "two rows in this file resolve to the same worker under different "
                    "names; obtain a distinct identifier from the agency"
                ),
            ))
            continue

        changes = registry.apply(result, as_of)
        seen_ids.add(result.worker.worker_id)
        # Joiner status is derived from first_seen, not from the NEW branch
        # alone, so a rerun of this period lists the same joiners and
        # events.py re-emits the same ids. The worker is then in joiners and
        # also in changed or unchanged. The membership check keeps a row
        # listed twice in one file from listing its worker twice.
        if result.worker.first_seen == as_of and result.worker not in diff.joiners:
            diff.joiners.append(result.worker)
        if changes:
            diff.changed.append((result.worker, changes))
        else:
            diff.unchanged.append(result.worker)

    for worker in registry.active_workers():
        if worker.worker_id in seen_ids:
            continue
        if registry.periods_missed(worker) >= absence_threshold:
            registry.deactivate(worker)
            diff.leavers.append(worker)

    return diff
