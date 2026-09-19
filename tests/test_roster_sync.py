import uuid
from datetime import date

import pytest

from roster_sync.diff import compute_diff
from roster_sync.identity import WorkerRegistry
from roster_sync.models import MatchConfidence, RosterRow
from roster_sync.normalize import (
    normalize_email,
    normalize_name,
    normalize_phone,
    normalize_role,
)

ROLE_MAP = {
    "material handler": "material_handler",
    "mh": "material_handler",
    "forklift operator": "forklift_operator",
}

WEEK_1 = date(2024, 7, 8)
WEEK_2 = date(2024, 7, 15)
WEEK_3 = date(2024, 7, 22)


# -- normalization --------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("(832) 555-0142", "+18325550142"),
        ("832.555.0142", "+18325550142"),
        ("8325550142", "+18325550142"),
        ("1-832-555-0142", "+18325550142"),
        (8325550142, "+18325550142"),
        ("8325550142.0", "+18325550142"),
        ("832-555-0142 x204", "+18325550142"),
    ],
)
def test_phone_formats_converge(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["n/a", "", None, "555", "0000000000", "pending"])
def test_unusable_phones_are_none(raw):
    assert normalize_phone(raw) is None


def test_email_normalization_and_rejection():
    assert normalize_email("  MWebb@Example.COM ") == "mwebb@example.com"
    assert normalize_email("no email") is None
    assert normalize_email("not-an-email") is None


def test_name_folding_ignores_case_accents_and_suffixes():
    a = normalize_name("Ray", "Villanueva Jr")
    b = normalize_name("  ray  ", "Villanueva")
    assert a == b

    assert normalize_name("José", "Peña") == normalize_name("jose", "pena")
    assert normalize_name("Mary", "O'Brien") == normalize_name("mary", "OBrien")


def test_name_handles_last_comma_first_in_one_cell():
    assert normalize_name("Webb, Marcus", "") == normalize_name("Marcus", "Webb")


def test_middle_name_does_not_change_identity():
    assert normalize_name("Marcus James", "Webb") == normalize_name("Marcus", "Webb")


def test_unmapped_role_returns_none_rather_than_defaulting():
    assert normalize_role("Material Handler", ROLE_MAP) == "material_handler"
    assert normalize_role("Site Marshal", ROLE_MAP) is None


# -- helpers --------------------------------------------------------------


def row(first, last, phone=None, email=None, role="Material Handler", source_row=2):
    return RosterRow(
        source_row=source_row,
        name=normalize_name(first, last),
        phone=normalize_phone(phone),
        email=normalize_email(email),
        role=normalize_role(role, ROLE_MAP),
    )


# -- identity -------------------------------------------------------------


def test_first_sighting_is_new_and_gets_an_id():
    registry = WorkerRegistry()
    result = registry.match(row("Marcus", "Webb", "(832) 555-0142", "mwebb@example.com"))
    assert result.confidence is MatchConfidence.NEW

    worker = registry.create(result.row, WEEK_1)
    assert worker.worker_id
    assert worker.role == "material_handler"


def test_worker_id_is_issued_not_derived_from_the_row():
    same = row("Marcus", "Webb", "(832) 555-0142", "mwebb@example.com")
    first = WorkerRegistry().create(same, WEEK_1)
    second = WorkerRegistry().create(same, WEEK_1)

    # An id derived from name, phone or email would be equal here, and would
    # change the day any of them changed.
    assert first.worker_id != second.worker_id
    assert uuid.UUID(first.worker_id).version == 4


def test_name_typo_still_matches_on_phone():
    registry = WorkerRegistry()
    registry.create(row("Marcus", "Webb", "(832) 555-0142", "mwebb@example.com"), WEEK_1)

    result = registry.match(row("Marcuss", "Webb", "832.555.0142", "mwebb@example.com"))
    assert result.confidence is MatchConfidence.STRONG_PHONE


def test_changed_phone_still_matches_on_email_and_keeps_id():
    registry = WorkerRegistry()
    first = registry.create(row("Danielle", "Okonkwo", "832.555.0178", "dok@example.com"), WEEK_1)

    result = registry.match(row("Danielle", "Okonkwo", "832-555-0301", "dok@example.com"))
    assert result.confidence is MatchConfidence.STRONG_EMAIL

    changes = registry.apply(result, WEEK_2)
    assert first.worker_id == result.worker.worker_id
    assert any("phone added" in c for c in changes)
    # Both numbers remain matchable afterwards.
    assert registry.match(row("Danielle", "Okonkwo", "832.555.0178")).confidence is MatchConfidence.STRONG_PHONE


def test_name_only_match_is_never_automatic():
    registry = WorkerRegistry()
    registry.create(row("Curtis", "Delaney", "832.555.0266", "cd@example.com"), WEEK_1)

    result = registry.match(row("Curtis", "Delaney", "832.555.0999", "other@example.com"))
    assert result.confidence is MatchConfidence.WEAK_NAME
    assert not result.confidence.is_automatic


def test_two_workers_sharing_a_name_escalate():
    registry = WorkerRegistry()
    registry.create(row("Chris", "Nguyen", "832.555.0101", "cn1@example.com"), WEEK_1)
    registry.create(row("Chris", "Nguyen", "832.555.0102", "cn2@example.com"), WEEK_1)

    result = registry.match(row("Chris", "Nguyen", "832.555.0999"))
    assert result.confidence is MatchConfidence.CONFLICT
    assert len(result.candidates) == 2


def test_reassigned_phone_pointing_at_another_name_escalates():
    registry = WorkerRegistry()
    registry.create(row("Ray", "Villanueva", "832.555.0193", "ray@example.com"), WEEK_1)
    registry.create(row("Alicia", "Fontenot", "832.555.0288", "af@example.com"), WEEK_1)

    # Fontenot's old number reissued to Villanueva by the carrier.
    result = registry.match(row("Ray", "Villanueva", "832.555.0288"))
    assert result.confidence is MatchConfidence.CONFLICT


def test_phone_and_email_pointing_at_different_workers_escalate():
    registry = WorkerRegistry()
    ray = registry.create(row("Ray", "Villanueva", "832.555.0193", "ray@example.com"), WEEK_1)
    alicia = registry.create(row("Alicia", "Fontenot", "832.555.0288", "af@example.com"), WEEK_1)

    # Ray's phone with Alicia's email. Neither signal outranks the other.
    result = registry.match(row("Ray", "Villanueva", "832.555.0193", "af@example.com"))
    assert result.confidence is MatchConfidence.CONFLICT
    assert result.worker is None
    assert result.candidates == [ray, alicia]


def test_row_without_any_contact_identifier_is_rejected():
    bad = row("Jerome", "Baptiste", "n/a", "no email")
    assert not bad.is_usable


# -- diffing --------------------------------------------------------------


def test_joiners_and_stable_ids_across_two_files():
    registry = WorkerRegistry()

    week1 = [
        row("Marcus", "Webb", "(832) 555-0142", "mwebb@example.com"),
        row("Tomas", "Ruiz", "8325550214", "truiz@example.com"),
    ]
    first = compute_diff(registry, week1, WEEK_1)
    assert first.summary()["joiners"] == 2
    webb_id = next(w.worker_id for w in first.joiners if w.name.last == "webb")

    week2 = [
        row("Marcuss", "Webb", "832.555.0142", "mwebb@example.com"),
        row("Tomas", "Ruiz", "8325550214", "truiz@example.com"),
        row("Alicia", "Fontenot", "832.555.0288", "af@example.com"),
    ]
    second = compute_diff(registry, week2, WEEK_2)
    assert second.summary()["joiners"] == 1
    assert second.summary()["leavers"] == 0
    # The typo'd name updated the record without minting a second worker.
    assert registry.get(webb_id).name.display == "Marcuss Webb"
    assert len(registry.workers) == 3


def test_single_absence_does_not_deactivate_but_two_do():
    registry = WorkerRegistry()
    present = row("Tomas", "Ruiz", "8325550214", "truiz@example.com")
    leaving = row("Ray", "Villanueva", "832.555.0193", "ray@example.com")

    compute_diff(registry, [present, leaving], WEEK_1)

    second = compute_diff(registry, [present], WEEK_2)
    assert second.summary()["leavers"] == 0, "one missing file must not deactivate a badge"

    third = compute_diff(registry, [present], WEEK_3)
    assert third.summary()["leavers"] == 1
    assert third.leavers[0].name.last == "villanueva"


def test_returning_worker_reactivates_under_the_same_id():
    registry = WorkerRegistry()
    present = row("Tomas", "Ruiz", "8325550214", "truiz@example.com")
    intermittent = row("Ray", "Villanueva", "832.555.0193", "ray@example.com")

    first = compute_diff(registry, [present, intermittent], WEEK_1)
    ray_id = next(w.worker_id for w in first.joiners if w.name.last == "villanueva")

    compute_diff(registry, [present], WEEK_2)
    compute_diff(registry, [present], WEEK_3)
    assert registry.get(ray_id).active is False

    fourth = compute_diff(registry, [present, intermittent], date(2024, 7, 29))
    assert fourth.summary()["joiners"] == 0, "a returning worker is not a new hire"
    assert registry.get(ray_id).active is True
    assert len(registry.workers) == 2


def test_role_change_is_reported_as_a_change():
    registry = WorkerRegistry()
    compute_diff(registry, [row("Tomas", "Ruiz", "8325550214", "truiz@example.com")], WEEK_1)

    promoted = row("Tomas", "Ruiz", "8325550214", "truiz@example.com", role="Forklift Operator")
    second = compute_diff(registry, [promoted], WEEK_2)

    assert second.summary()["changed"] == 1
    worker, changes = second.changed[0]
    assert worker.role == "forklift_operator"
    assert any("role" in c for c in changes)


def test_uncertain_rows_go_to_review_and_provision_nothing():
    registry = WorkerRegistry()
    compute_diff(registry, [row("Curtis", "Delaney", "832.555.0266", "cd@example.com")], WEEK_1)

    ambiguous = row("Curtis", "Delaney", "832.555.0999", "different@example.com")
    second = compute_diff(registry, [ambiguous], WEEK_2)

    assert second.summary()["review"] == 1
    assert second.summary()["joiners"] == 0
    assert len(registry.workers) == 1


def test_conflicting_strong_signals_go_to_review_and_change_neither_worker():
    registry = WorkerRegistry()
    first = compute_diff(
        registry,
        [
            row("Ray", "Villanueva", "832.555.0193", "ray@example.com"),
            row("Alicia", "Fontenot", "832.555.0288", "af@example.com"),
        ],
        WEEK_1,
    )
    ray, alicia = first.joiners

    # Resolving by precedence would apply this row to Ray and hand him
    # Alicia's email.
    mixed = row("Ray", "Villanueva", "832.555.0193", "af@example.com")
    second = compute_diff(registry, [mixed], WEEK_2)

    assert second.summary()["review"] == 1
    assert second.summary()["changed"] == 0
    assert (ray.phones, ray.emails) == ({"+18325550193"}, {"ray@example.com"})
    assert (alicia.phones, alicia.emails) == ({"+18325550288"}, {"af@example.com"})
