import sqlite3
from datetime import date

import pytest

from roster_sync.diff import compute_diff
from roster_sync.identity import WorkerRegistry
from roster_sync.models import MatchConfidence, RosterRow, Worker
from roster_sync.normalize import normalize_email, normalize_name, normalize_phone
from roster_sync.review import ReviewResolutionError, confirm, reject
from roster_sync.store import Store, file_hash

WEEK_1 = date(2024, 7, 8)
WEEK_2 = date(2024, 7, 15)
WEEK_3 = date(2024, 7, 22)


def row(first, last, phone=None, email=None, role="material_handler", source_row=2):
    return RosterRow(
        source_row=source_row,
        name=normalize_name(first, last),
        phone=normalize_phone(phone),
        email=normalize_email(email),
        role=role,
    )


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "roster.db") as s:
        yield s


# -- persistence ----------------------------------------------------------


def test_registry_survives_a_restart_with_identifiers_intact(tmp_path):
    path = tmp_path / "roster.db"

    with Store(path) as first_run:
        registry = first_run.load_registry()
        compute_diff(registry, [row("Marcus", "Webb", "(832) 555-0142", "mw@example.com")], WEEK_1)
        first_run.record_period(WEEK_1)
        first_run.save_registry(registry)
        webb_id = registry.workers[0].worker_id

    # A separate process next week sees the same population.
    with Store(path) as second_run:
        registry = second_run.load_registry()
        assert len(registry.workers) == 1
        assert registry.periods == [WEEK_1]

        diff = compute_diff(registry, [row("Marcuss", "Webb", "832.555.0142", "mw@example.com")], WEEK_2)
        second_run.record_period(WEEK_2)
        second_run.save_registry(registry)

        assert diff.summary()["joiners"] == 0
        assert registry.workers[0].worker_id == webb_id


def test_reprocessing_the_same_file_does_not_deactivate_anyone():
    registry = WorkerRegistry()
    present = row("Tomas", "Ruiz", "8325550214", "tr@example.com")
    absent = row("Ray", "Villanueva", "832.555.0193", "rv@example.com")

    compute_diff(registry, [present, absent], WEEK_1)
    compute_diff(registry, [present], WEEK_2)

    # The week 2 job is retried after a transient failure.
    rerun = compute_diff(registry, [present], WEEK_2)

    assert rerun.is_rerun is True
    assert rerun.summary()["leavers"] == 0, "a retry must not age an absent worker"

    third = compute_diff(registry, [present], WEEK_3)
    assert third.summary()["leavers"] == 1


def test_retried_job_in_a_new_process_does_not_deactivate_anyone(tmp_path):
    path = tmp_path / "roster.db"
    present = row("Tomas", "Ruiz", "8325550214", "tr@example.com")
    absent = row("Ray", "Villanueva", "832.555.0193", "rv@example.com")

    with Store(path) as week1:
        registry = week1.load_registry()
        compute_diff(registry, [present, absent], WEEK_1)
        week1.record_period(WEEK_1)
        week1.save_registry(registry)

    with Store(path) as week2:
        registry = week2.load_registry()
        compute_diff(registry, [present], WEEK_2)
        week2.record_period(WEEK_2)
        week2.save_registry(registry)

    # The week 2 job runs again in a fresh process; all it knows is the database.
    with Store(path) as retry:
        registry = retry.load_registry()
        rerun = compute_diff(registry, [present], WEEK_2)
        retry.record_period(WEEK_2)
        retry.save_registry(registry)

        assert rerun.is_rerun is True
        assert rerun.summary()["leavers"] == 0, "a retry must not age an absent worker"

    with Store(path) as week3:
        registry = week3.load_registry()
        third = compute_diff(registry, [present], WEEK_3)
        assert third.summary()["leavers"] == 1


def test_record_period_is_idempotent_and_keeps_the_first_hash(tmp_path, store):
    file_a = tmp_path / "a.txt"
    file_a.write_text("roster contents")

    digest = file_hash(file_a)
    assert store.record_period(WEEK_1, str(file_a), digest) is True
    assert store.record_period(WEEK_1, str(file_a), digest) is False
    assert store.period_hash(WEEK_1) == digest


# -- review queue ---------------------------------------------------------


def test_flag_persists_and_is_described_for_a_human(store):
    registry = store.load_registry()
    compute_diff(registry, [row("Curtis", "Delaney", "832.555.0266", "cd@example.com")], WEEK_1)
    store.save_registry(registry)

    ambiguous = row("Curtis", "Delaney", "832.555.0999", "different@example.com")
    diff = compute_diff(registry, [ambiguous], WEEK_2)
    store.save_reviews(diff.review, WEEK_2)

    open_items = store.open_reviews()
    assert len(open_items) == 1
    description = open_items[0].describe(registry)
    assert "Curtis Delaney" in description
    assert "weak_name" in description


def test_saving_the_same_flag_twice_does_not_duplicate_it(store):
    registry = store.load_registry()
    compute_diff(registry, [row("Curtis", "Delaney", "832.555.0266", "cd@example.com")], WEEK_1)

    ambiguous = row("Curtis", "Delaney", "832.555.0999", "different@example.com")
    diff = compute_diff(registry, [ambiguous], WEEK_2)

    created = store.save_reviews(diff.review, WEEK_2)
    assert store.save_reviews(diff.review, WEEK_2) == [], "the second call creates nothing"

    assert len(created) == 1
    assert [r.review_id for r in store.open_reviews()] == created


def test_confirming_merges_identifiers_so_the_flag_never_returns(store):
    registry = store.load_registry()
    compute_diff(registry, [row("Curtis", "Delaney", "832.555.0266", "cd@example.com")], WEEK_1)
    store.save_registry(registry)
    curtis_id = registry.workers[0].worker_id

    ambiguous = row("Curtis", "Delaney", "832.555.0999", "new@example.com")
    diff = compute_diff(registry, [ambiguous], WEEK_2)
    store.save_reviews(diff.review, WEEK_2)
    review_id = store.open_reviews()[0].review_id

    resolution = confirm(store, registry, review_id, curtis_id, decided_by="a.diaz")

    assert resolution.decision == "confirmed"
    assert any("phone added" in c for c in resolution.changes)
    assert store.open_reviews() == []

    # The same row next week now matches on a strong signal.
    third = compute_diff(registry, [ambiguous], WEEK_3)
    assert third.summary()["review"] == 0
    assert third.summary()["unchanged"] == 1
    assert len(registry.workers) == 1


def test_confirming_dates_the_sighting_to_the_period_that_carried_the_row(store):
    registry = store.load_registry()
    compute_diff(registry, [row("Curtis", "Delaney", "832.555.0266")], WEEK_1)
    store.save_registry(registry)
    curtis = registry.workers[0]

    # Flagged directly, so nothing but confirm can move last_seen off week 1.
    flagged = registry.match(row("Curtis", "Delaney", "832.555.0999"))
    (review_id,) = store.save_reviews([flagged], WEEK_2)

    confirm(store, registry, review_id, curtis.worker_id, decided_by="a.diaz")

    # Absence is derived from roster periods, so the sighting carries the
    # period of the file, not the day somebody answered the flag.
    assert curtis.last_seen == WEEK_2


def test_rejecting_creates_a_second_person_deliberately(store):
    registry = store.load_registry()
    compute_diff(registry, [row("Chris", "Nguyen", "832.555.0101", "cn1@example.com")], WEEK_1)
    store.save_registry(registry)

    other = row("Chris", "Nguyen", "832.555.0202", "cn2@example.com")
    diff = compute_diff(registry, [other], WEEK_2)
    store.save_reviews(diff.review, WEEK_2)
    review_id = store.open_reviews()[0].review_id

    resolution = reject(store, registry, review_id, decided_by="a.diaz")

    assert resolution.decision == "rejected"
    assert len(registry.workers) == 2
    assert store.open_reviews() == []

    third = compute_diff(registry, [other], WEEK_3)
    assert third.summary()["review"] == 0


def test_decision_is_attributed_and_survives_a_restart(tmp_path):
    path = tmp_path / "roster.db"
    with Store(path) as first:
        registry = first.load_registry()
        compute_diff(registry, [row("Curtis", "Delaney", "832.555.0266", "cd@example.com")], WEEK_1)
        first.save_registry(registry)
        worker_id = registry.workers[0].worker_id

        diff = compute_diff(registry, [row("Curtis", "Delaney", "832.555.0999", "n@example.com")], WEEK_2)
        first.save_reviews(diff.review, WEEK_2)
        review_id = first.open_reviews()[0].review_id
        confirm(first, registry, review_id, worker_id, decided_by="a.diaz")

    with Store(path) as second:
        registry = second.load_registry()
        assert second.open_reviews() == []
        decided = second.get_review(review_id)
        assert (decided.decision, decided.decided_by, decided.decided_worker_id) == (
            "confirmed", "a.diaz", worker_id)
        assert decided.decided_at is not None
        # The merged phone came back with the worker.
        assert "+18325550999" in registry.workers[0].phones

        # Deciding again, by either route, must not replace who decided first.
        with pytest.raises(ReviewResolutionError, match="already confirmed"):
            confirm(second, registry, review_id, worker_id, decided_by="someone.else")
        with pytest.raises(ReviewResolutionError, match="already confirmed"):
            reject(second, registry, review_id, decided_by="someone.else")
        assert second.get_review(review_id).decided_by == "a.diaz"


def flag_the_same_row_in_two_weeks(store):
    """Two open flags for one question: the review id includes the period."""
    registry = store.load_registry()
    compute_diff(registry, [row("Curtis", "Delaney", "832.555.0266", "cd@example.com")], WEEK_1)
    store.save_registry(registry)

    ambiguous = row("Curtis", "Delaney", "832.555.0999")
    flags = []
    for week in (WEEK_2, WEEK_3):
        diff = compute_diff(registry, [ambiguous], week)
        flags += store.save_reviews(diff.review, week)
    return registry, flags


def test_rejecting_a_stale_flag_whose_duplicate_was_confirmed_is_refused(store):
    registry, (week2_flag, week3_flag) = flag_the_same_row_in_two_weeks(store)
    assert len(store.open_reviews()) == 2
    curtis = registry.workers[0]

    confirm(store, registry, week2_flag, curtis.worker_id, decided_by="a.diaz")

    # The phone is Curtis's now. Rejecting would mint a second Curtis holding it.
    with pytest.raises(ReviewResolutionError):
        reject(store, registry, week3_flag, decided_by="a.diaz")
    assert len(registry.workers) == 1


def test_confirming_a_stale_flag_whose_duplicate_was_rejected_is_refused(store):
    registry, (week2_flag, week3_flag) = flag_the_same_row_in_two_weeks(store)
    assert len(store.open_reviews()) == 2
    curtis = registry.workers[0]

    reject(store, registry, week2_flag, decided_by="a.diaz")

    # The phone belongs to the second Curtis now. It must not also merge into the first.
    with pytest.raises(ReviewResolutionError):
        confirm(store, registry, week3_flag, curtis.worker_id, decided_by="a.diaz")
    assert "+18325550999" not in curtis.phones


def test_resolving_an_unknown_review_is_refused(store):
    with pytest.raises(ReviewResolutionError, match="no review"):
        reject(store, store.load_registry(), "no-such-review", decided_by="a.diaz")


def test_confirming_onto_an_unknown_worker_is_refused(store):
    registry, (week2_flag, _) = flag_the_same_row_in_two_weeks(store)
    with pytest.raises(ReviewResolutionError, match="no worker"):
        confirm(store, registry, week2_flag, "no-such-worker", decided_by="a.diaz")


def test_store_keeps_the_first_decision_when_asked_to_record_a_second(store):
    registry, (week2_flag, _) = flag_the_same_row_in_two_weeks(store)
    curtis_id = registry.workers[0].worker_id

    # The backstop under review.py: written straight to the store, past its check.
    store.record_decision(week2_flag, "confirmed", curtis_id, "a.diaz")
    store.record_decision(week2_flag, "rejected", None, "someone.else")

    decided = store.get_review(week2_flag)
    assert (decided.decision, decided.decided_worker_id, decided.decided_by) == (
        "confirmed", curtis_id, "a.diaz")


def test_conflict_flag_cannot_give_a_held_phone_a_second_owner(tmp_path):
    path = tmp_path / "roster.db"
    with Store(path) as first:
        registry = first.load_registry()
        week_1 = [
            row("Chris", "Nguyen", "832.555.0101", "cn@example.com"),
            row("Alicia", "Fontenot", "832.555.0177", "af@example.com"),
        ]
        compute_diff(registry, week_1, WEEK_1)
        first.save_registry(registry)
        nguyen, fontenot = registry.workers

        # Nguyen's phone under Fontenot's name: the phone and the name point at different workers.
        diff = compute_diff(registry, [row("Alicia", "Fontenot", "832.555.0101")], WEEK_2)
        first.save_reviews(diff.review, WEEK_2)
        open_items = first.open_reviews()
        assert len(open_items) == 1
        assert open_items[0].confidence is MatchConfidence.CONFLICT
        review_id = open_items[0].review_id

        with pytest.raises(ReviewResolutionError):
            confirm(first, registry, review_id, fontenot.worker_id, decided_by="a.diaz")
        assert "+18325550101" not in fontenot.phones

        with pytest.raises(ReviewResolutionError):
            reject(first, registry, review_id, decided_by="a.diaz")
        assert len(registry.workers) == 2

    with Store(path) as second:
        reloaded = second.load_registry()
        assert reloaded.get(nguyen.worker_id).phones == {"+18325550101"}
        assert [r.review_id for r in second.open_reviews()] == [review_id]


def test_flag_whose_phone_and_email_have_different_owners_leaves_both_intact(tmp_path):
    path = tmp_path / "roster.db"
    with Store(path) as first:
        registry = first.load_registry()
        week_1 = [row("Ana", "Reyes", "832.555.0111"), row("Ben", "Okafor", email="bo@example.com")]
        compute_diff(registry, week_1, WEEK_1)
        first.save_registry(registry)
        ana, ben = registry.workers

        clash = row("Cal", "Unger", "832.555.0111", "bo@example.com")
        diff = compute_diff(registry, [clash], WEEK_2)
        first.save_reviews(diff.review, WEEK_2)
        (review,) = first.open_reviews()

        with pytest.raises(ReviewResolutionError):
            reject(first, registry, review.review_id, decided_by="a.diaz")
        assert len(registry.workers) == 2

        # Ana owns the phone, but confirming onto her would hand her Ben's email.
        with pytest.raises(ReviewResolutionError, match="bo@example.com already belongs to Ben Okafor"):
            confirm(first, registry, review.review_id, ana.worker_id, decided_by="a.diaz")

    with Store(path) as second:
        reloaded = second.load_registry()
        assert reloaded.get(ana.worker_id).phones == {"+18325550111"}
        assert reloaded.get(ben.worker_id).emails == {"bo@example.com"}


def test_store_refuses_to_write_one_identifier_under_two_workers(store):
    # The backstop under the review guard: a registry that reached double ownership some other way.
    registry = WorkerRegistry()
    registry.adopt(Worker("worker-a", normalize_name("Ana", "Reyes"), phones={"+18325550111"}))
    registry.adopt(Worker("worker-b", normalize_name("Ben", "Okafor"), phones={"+18325550111"}))

    with pytest.raises(sqlite3.IntegrityError):
        store.save_registry(registry)
    assert store.load_registry().workers == [], "the whole save rolls back, worker rows included"


def test_unresolved_flag_does_not_age_a_worker_into_a_false_leaver(store):
    registry = store.load_registry()
    compute_diff(registry, [row("Curtis", "Delaney", "832.555.0266", "cd@example.com")], WEEK_1)

    # Curtis appears every week, but always in a form that needs review.
    ambiguous = row("Curtis", "Delaney", "832.555.0999", "new@example.com")
    compute_diff(registry, [ambiguous], WEEK_2)
    third = compute_diff(registry, [ambiguous], WEEK_3)

    assert third.summary()["leavers"] == 0, "a pending flag must not deactivate a badge"
    assert registry.workers[0].active is True


def test_flag_naming_only_candidates_does_not_age_either_of_them():
    registry = WorkerRegistry()
    compute_diff(registry, [row("Chris", "Nguyen", "832.555.0101", "cn1@example.com")], WEEK_1)
    # Created directly: compute_diff would flag a second Chris Nguyen as
    # WEAK_NAME rather than create him.
    registry.create(row("Chris", "Nguyen", "832.555.0102", "cn2@example.com"), WEEK_1)

    # The name matches both, so the flag carries two candidates and no worker.
    ambiguous = row("Chris", "Nguyen", "832.555.0999")
    compute_diff(registry, [ambiguous], WEEK_2)
    third = compute_diff(registry, [ambiguous], WEEK_3)

    assert third.review[0].worker is None
    assert third.summary()["leavers"] == 0, "either candidate may be the person on site"
    assert all(w.active for w in registry.workers)
