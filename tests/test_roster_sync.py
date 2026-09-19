import ast
import uuid
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

import roster_sync
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
WEEK_4 = date(2024, 7, 29)


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
        ("832-555-0142 ext. 204", "+18325550142"),
        ("832-555-0142 ext204", "+18325550142"),
        ("+1 (832) 555-0142", "+18325550142"),
    ],
)
def test_phone_formats_converge(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "n/a", "", None, "555", "0000000000", "pending",
        # Eleven digits that do not start with the country code: refused, not guessed.
        "28325550142", "44 832 555 0142",
    ],
)
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


@pytest.mark.parametrize("written,plain", [("Smith-Jones", "Smith Jones"), ("Villanueva Jr.", "Villanueva")])
def test_hyphenated_and_dotted_suffix_last_names_converge(written, plain):
    assert normalize_name("Ray", written).last == normalize_name("Ray", plain).last


@pytest.mark.parametrize("first,last", [
    ("", "Webb"), ("Marcus", None), ("n/a", "Webb"),
    ("Marcus", "--"), ("Marcus", "TBD"), ("Unknown", "Webb"), ("Marcus", "null"),
])
def test_name_with_a_missing_part_is_none(first, last):
    # A worker is never created with an empty first or last name.
    assert normalize_name(first, last) is None


# "na" and "x" are placeholders in a contact cell and real names in a name
# cell; "v" is a generational suffix only when a surname is left without it.
@pytest.mark.parametrize("first,last,key", [
    ("Na", "Li", "li|na"),
    ("Li", "Na", "na|li"),
    ("X", "Webb", "webb|x"),
    ("Marcus", "V", "v|marcus"),
    ("Marcus", "Webb V", "webb|marcus"),
])
def test_short_names_are_not_mistaken_for_placeholders_or_suffixes(first, last, key):
    name = normalize_name(first, last)
    assert name is not None and name.key == key


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


def test_released_identifiers_have_no_owner_until_a_row_is_applied_again():
    registry = WorkerRegistry()
    alicia = registry.create(row("Alicia", "Fontenot", "832.555.0288", "af@example.com"), WEEK_1)
    registry.apply(registry.match(row("Alicia", "Fontenot", "832.555.0301", "af@example.com")), WEEK_2)
    reissued = row("Ray", "Villanueva", "832.555.0288", "af@example.com")

    assert registry.release(alicia, reissued) == [("phone", "+18325550288"), ("email", "af@example.com")]

    # Only what the row carries, and the set and the index together: an
    # identifier left in either index would still match Alicia.
    assert (alicia.phones, alicia.emails) == ({"+18325550301"}, set())
    assert registry.owners_of(reissued) == []
    assert registry.match(reissued).confidence is MatchConfidence.NEW


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


def test_backfilled_older_file_does_not_move_last_seen_backwards():
    registry = WorkerRegistry()
    present = row("Tomas", "Ruiz", "8325550214", "truiz@example.com")
    ray = row("Ray", "Villanueva", "832.555.0193", "ray@example.com")

    first = compute_diff(registry, [present, ray], WEEK_1)
    villanueva = next(w for w in first.joiners if w.name.last == "villanueva")
    compute_diff(registry, [present, ray], WEEK_3)
    # Week 2 arrives late. Ray is on it, as he was on every file so far.
    compute_diff(registry, [present, ray], WEEK_2)
    assert villanueva.last_seen == WEEK_3

    # His first real absence. Counted from week 2 it would look like his second.
    fourth = compute_diff(registry, [present], WEEK_4)
    assert fourth.summary()["leavers"] == 0, "one missing file must not deactivate a badge"
    assert villanueva.active is True


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


def test_unmapped_role_unsets_the_role_once_and_a_rerun_reports_no_change():
    registry = WorkerRegistry()
    compute_diff(registry, [row("Tomas", "Ruiz", "8325550214")], WEEK_1)
    # read_roster sets the flag; row() builds the RosterRow by hand.
    relabelled = replace(row("Tomas", "Ruiz", "8325550214", role="Reach Truck Operator"), role_unmapped=True)

    second = compute_diff(registry, [relabelled], WEEK_2)
    ((worker, changes),) = second.changed
    assert worker.role is None
    assert changes == ["role material_handler -> unmapped"]

    rerun = compute_diff(registry, [relabelled], WEEK_2)
    assert rerun.summary()["changed"] == 0, "the change is recorded once, not on every rerun"


def test_empty_role_cell_leaves_the_role_unchanged():
    registry = WorkerRegistry()
    compute_diff(registry, [row("Tomas", "Ruiz", "8325550214")], WEEK_1)

    # An omitted cell says nothing about the role; only text the map cannot read does.
    second = compute_diff(registry, [row("Tomas", "Ruiz", "8325550214", role="")], WEEK_2)
    assert second.summary()["unchanged"] == 1
    assert registry.workers[0].role == "material_handler"


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


def test_one_phone_under_two_names_in_one_file_goes_to_review_and_renames_nobody():
    registry = WorkerRegistry()
    shared_phone = [
        row("Maria", "Lopez", "832-555-0111"),
        row("Jose", "Lopez", "832-555-0111", source_row=3),
    ]
    diff = compute_diff(registry, shared_phone, WEEK_1)

    # Applying the second row would rename Maria and put two people on one badge.
    assert diff.summary() == {"joiners": 1, "leavers": 0, "changed": 0,
                              "unchanged": 0, "review": 1, "rejected": 0}
    assert [w.name.display for w in registry.workers] == ["Maria Lopez"]

    (flag,) = diff.review
    assert flag.confidence is MatchConfidence.CONFLICT
    assert flag.row.name.display == "Jose Lopez"
    assert flag.worker is diff.joiners[0]
    assert flag.candidates == [flag.worker]


def test_same_row_twice_in_one_file_is_applied_and_not_flagged():
    registry = WorkerRegistry()
    twice = [
        row("Maria", "Lopez", "832-555-0111"),
        row("Maria", "Lopez", "832-555-0111", source_row=3),
    ]
    diff = compute_diff(registry, twice, WEEK_1)

    # One person listed twice is a copy-paste slip, not a question for a human.
    assert diff.summary()["review"] == 0
    assert diff.summary()["unchanged"] == 1
    assert len(registry.workers) == 1


def test_same_row_twice_in_one_file_lists_the_joiner_once():
    registry = WorkerRegistry()
    twice = [
        row("Maria", "Lopez", "832-555-0111"),
        row("Maria", "Lopez", "832-555-0111", source_row=3),
    ]
    first = compute_diff(registry, twice, WEEK_1)
    rerun = compute_diff(registry, twice, WEEK_1)

    # The second row is a strong match on a worker first seen this period,
    # which is exactly the derived-joiner condition.
    assert len(first.joiners) == 1
    assert rerun.joiners == first.joiners


# -- package root ---------------------------------------------------------


def test_package_root_exports_exactly_the_names_the_repository_imports_from_it():
    repo = Path(__file__).resolve().parent.parent
    used = set()
    for path in [*repo.glob("samples/*.py"), *repo.glob("tests/*.py")]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "roster_sync":
                used.update(alias.name for alias in node.names)

    # A root name with no importer is a second index of the package, kept in
    # step by hand. A name returns to the root when something imports it there.
    assert set(roster_sync.__all__) == used
    assert roster_sync.__all__ == sorted(roster_sync.__all__)
    assert all(hasattr(roster_sync, name) for name in roster_sync.__all__)
