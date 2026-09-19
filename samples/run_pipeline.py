"""End-to-end demonstration of the whole pipeline.

Run: python samples/run_pipeline.py

Processes two roster files, grants credentials, evaluates eligibility,
provisions access, reconciles a pay period across three hours sources, then
emits each week's worker.joined events and reruns the last week to show the
same event ids come out. Writes to a throwaway database so it can be run
repeatedly.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from roster_sync import (  # noqa: E402
    Credential, InMemoryEventSender, InMemoryProvisioner, Store, build_report,
    compute_diff, emit_diff, file_hash, read_agency_report, read_punch_log,
    read_site_feed, read_roster, reconcile, sync_access,
)
from roster_sync.hours import CLEAN  # noqa: E402

DB = ROOT / "demo.db"
SAMPLES = ROOT / "samples"
with open(ROOT / "config" / "roles.yaml") as handle:
    CONFIG = yaml.safe_load(handle)
ROLES, REQUIREMENTS = CONFIG["aliases"], CONFIG["requirements"]

WEEK_1, WEEK_2 = date(2024, 7, 8), date(2024, 7, 15)
DAY_1, DAY_2 = date(2024, 7, 15), date(2024, 7, 16)


def rule(text: str) -> None:
    print(f"\n{'=' * 68}\n{text}\n{'=' * 68}")


def main() -> None:
    if DB.exists():
        DB.unlink()

    with Store(DB) as store:
        rule("1. Roster ingest and identity resolution")
        registry = store.load_registry()
        weeks = []
        for path, as_of in [(SAMPLES / "roster_week1.xlsx", WEEK_1),
                            (SAMPLES / "roster_week2.xlsx", WEEK_2)]:
            rows, report = read_roster(path, ROLES)
            diff = compute_diff(registry, rows, as_of)
            weeks.append((rows, diff))
            store.record_period(as_of, str(path), file_hash(path))
            store.save_registry(registry)
            store.save_reviews(diff.review, as_of)
            print(f"\n{path.name}  header row {report.header_row}  "
                  f"{report.rows_read} rows  rerun={diff.is_rerun}")
            print(f"  {diff.summary()}")
            for worker in diff.joiners:
                print(f"    JOINER  {worker.name.display:22} {worker.role}")
            for worker, changes in diff.changed:
                print(f"    CHANGED {worker.name.display:22} {'; '.join(changes)}")

        rule("2. Credentials granted")
        by_last = {w.name.last: w for w in registry.workers}
        ruiz, fontenot, baptiste = by_last["ruiz"], by_last["fontenot"], by_last["baptiste"]
        priya, villanueva = by_last["raghunathan"], by_last["villanueva"]

        for worker in registry.workers:
            store.grant_credential(Credential(worker.worker_id, "safety_orientation", WEEK_1))
            if worker.worker_id != baptiste.worker_id:
                store.grant_credential(Credential(worker.worker_id, "ppe_issued", WEEK_1))
        # Baptiste is a forklift operator with no PPE record and no certificate.
        # Raghunathan's buckhoist training expires inside the warning window.
        store.grant_credential(Credential(priya.worker_id, "buckhoist_training", WEEK_1,
                                          expires_on=DAY_1 + timedelta(days=9)))
        store.grant_credential(Credential(villanueva.worker_id, "forklift_certification", WEEK_1,
                                          expires_on=DAY_1 + timedelta(days=200)))
        print(f"  {sum(len(v) for v in store.all_credentials().values())} credential records")

        rule("3. Eligibility gate")
        report = build_report(registry.workers, store.all_credentials(), REQUIREMENTS, DAY_1)
        print(f"  {report.summary()}\n")
        for verdict in report.blocked + report.warned:
            print("  " + verdict.describe())

        rule("4. Access provisioning")
        provisioner = InMemoryProvisioner()
        inactive = [w for w in registry.workers if not w.active]
        first = sync_access(report, store, provisioner, inactive)
        calls_first = len(provisioner.calls)
        print(f"  first run:  {first.summary()}   api calls: {calls_first}")
        second = sync_access(report, store, provisioner, inactive)
        # provisioner.calls is cumulative, so the rerun prints its own share.
        calls_rerun = len(provisioner.calls) - calls_first
        print(f"  rerun:      {second.summary()}   api calls: {calls_rerun}")

        for worker_id, ref in provisioner.active.items():
            store.map_badge(ref, worker_id, DAY_1)

        rule("5. Three-way hours reconciliation")
        (SAMPLES / "scanner_punches.csv").write_text(
            "worker_id,date,in,out\n"
            f"{ruiz.worker_id},2024-07-15,06:00,14:00\n"
            f"{ruiz.worker_id},2024-07-16,06:00,14:00\n"
            f"{fontenot.worker_id},2024-07-15,06:00,14:00\n"
            f"{fontenot.worker_id},2024-07-16,06:00,12:00\n"
        )
        badge_of = {w: b for b, w in store.badge_map().items()}
        ruiz_badge, fontenot_badge = badge_of[ruiz.worker_id], badge_of[fontenot.worker_id]
        (SAMPLES / "site_badge_feed.csv").write_text(
            "badge_id,date,in,out\n"
            f"{ruiz_badge},2024-07-15,05:52,14:05\n"
            f"{ruiz_badge},2024-07-16,05:55,14:02\n"
            f"{fontenot_badge},2024-07-16,06:01,12:03\n"
            "BADGE-UNKNOWN,2024-07-15,06:00,14:00\n"
        )

        agency, unresolved_a = read_agency_report(SAMPLES / "agency_hours.csv", registry)
        scanner, unresolved_s = read_punch_log(SAMPLES / "scanner_punches.csv", "scanner")
        site, unresolved_x = read_site_feed(SAMPLES / "site_badge_feed.csv", store.badge_map())

        recon = reconcile(agency, scanner, site, DAY_1, DAY_2,
                          unresolved=unresolved_a + unresolved_s + unresolved_x)
        print(f"  {recon.summary()}\n")
        names = {w.worker_id: w.name.display for w in registry.workers}
        for variance in sorted(recon.workers, key=lambda v: names.get(v.worker_id, v.worker_id)):
            print(f"  {names.get(variance.worker_id, variance.worker_id):22} "
                  f"agency {variance.agency:5.2f}  scanner {variance.scanner:5.2f}  "
                  f"site {variance.site if variance.site is None else f'{variance.site:5.2f}'}")
            for day in variance.days:
                if day.reading != CLEAN:
                    print(f"      {day.day}  {day.reading}  "
                          f"(agency {day.agency}, scanner {day.scanner}, site {day.site})")
        for item in recon.unresolved:
            print(f"  UNRESOLVED [{item.source}] {item.reason}")

        rule("6. Event emission to the iPaaS")
        # A production run emits a period's events straight after its diff;
        # the demo holds them until here so each step prints as one block.
        outcomes = [emit_diff(diff, InMemoryEventSender()) for _, diff in weeks]
        for (_, diff), outcome in zip(weeks, outcomes):
            print(f"  {diff.as_of}  rerun={diff.is_rerun!s:<5}  {outcome.summary()}")
        # A retried job starts from the store, so the rerun does too. It derives
        # the same joiners, so the same ids go out and the receiver discards
        # them. The ids are compared and not printed: each one hashes a worker
        # id that is issued fresh on every demo run.
        rows, last = weeks[-1]
        rerun = compute_diff(store.load_registry(), rows, last.as_of)
        resent = emit_diff(rerun, InMemoryEventSender())
        print(f"  {rerun.as_of}  rerun={rerun.is_rerun!s:<5}  {resent.summary()}  "
              f"ids identical on rerun: {resent.sent == outcomes[-1].sent}")

    DB.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
