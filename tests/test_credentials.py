from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from roster_sync.credentials import Credential, CredentialStatus, build_report, evaluate
from roster_sync.diff import compute_diff
from roster_sync.models import RosterRow, Worker
from roster_sync.normalize import (
    NormalizedName, normalize_email, normalize_name, normalize_phone, normalize_role,
)
from roster_sync.store import Store

TODAY = date(2024, 7, 15)

# Resolved from this file, not the cwd, so the suite runs from any directory.
ROLES_YAML = Path(__file__).resolve().parent.parent / "config" / "roles.yaml"
with open(ROLES_YAML) as handle:
    CONFIG = yaml.safe_load(handle)
REQUIREMENTS = CONFIG["requirements"]


def worker(role, wid="w-1", active=True):
    return Worker(worker_id=wid, name=NormalizedName("tomas", "ruiz"), role=role, active=active)


def cred(kind, wid="w-1", granted=TODAY - timedelta(days=30), expires=None):
    return Credential(worker_id=wid, kind=kind, granted_on=granted, expires_on=expires)


# -- status ---------------------------------------------------------------


def test_status_transitions_around_expiry():
    c = cred("forklift_certification", expires=TODAY + timedelta(days=60))
    assert c.status_on(TODAY, warn_days=14) is CredentialStatus.VALID
    assert c.status_on(TODAY + timedelta(days=50), warn_days=14) is CredentialStatus.EXPIRING
    assert c.status_on(TODAY + timedelta(days=61), warn_days=14) is CredentialStatus.EXPIRED


def test_credential_without_expiry_never_expires():
    assert cred("ppe_issued").status_on(date(2030, 1, 1), 14) is CredentialStatus.VALID


# -- gate -----------------------------------------------------------------


def test_material_handler_cleared_with_orientation_and_ppe():
    v = evaluate(worker("material_handler"), [cred("safety_orientation"), cred("ppe_issued")], REQUIREMENTS, TODAY)
    assert v.cleared
    assert v.blocking == []


def test_missing_credential_blocks_and_is_named():
    v = evaluate(worker("material_handler"), [cred("safety_orientation")], REQUIREMENTS, TODAY)
    assert not v.cleared
    assert [f.kind for f in v.blocking] == ["ppe_issued"]
    assert v.blocking[0].status is CredentialStatus.MISSING


def test_forklift_operator_needs_certification_on_top_of_basics():
    basics = [cred("safety_orientation"), cred("ppe_issued")]
    assert not evaluate(worker("forklift_operator"), basics, REQUIREMENTS, TODAY).cleared

    with_cert = basics + [cred("forklift_certification", expires=TODAY + timedelta(days=300))]
    assert evaluate(worker("forklift_operator"), with_cert, REQUIREMENTS, TODAY).cleared


def test_expired_certification_blocks():
    held = [cred("safety_orientation"), cred("ppe_issued"),
            cred("forklift_certification", expires=TODAY - timedelta(days=1))]
    v = evaluate(worker("forklift_operator"), held, REQUIREMENTS, TODAY)
    assert not v.cleared
    assert v.blocking[0].status is CredentialStatus.EXPIRED


def test_expiring_certification_clears_with_a_warning():
    held = [cred("safety_orientation"), cred("ppe_issued"),
            cred("forklift_certification", expires=TODAY + timedelta(days=10))]
    v = evaluate(worker("forklift_operator"), held, REQUIREMENTS, TODAY, warn_days=14)
    assert v.cleared
    assert [f.kind for f in v.warnings] == ["forklift_certification"]


def test_renewal_supersedes_the_lapsed_certificate():
    held = [
        cred("safety_orientation"), cred("ppe_issued"),
        cred("forklift_certification", granted=TODAY - timedelta(days=400), expires=TODAY - timedelta(days=35)),
        cred("forklift_certification", granted=TODAY - timedelta(days=30), expires=TODAY + timedelta(days=335)),
    ]
    v = evaluate(worker("forklift_operator"), held, REQUIREMENTS, TODAY)
    assert v.cleared, "the newer certificate must win over the expired one"


def test_unmapped_role_blocks_rather_than_defaulting():
    v = evaluate(worker(None), [cred("safety_orientation"), cred("ppe_issued")], REQUIREMENTS, TODAY)
    assert not v.cleared
    assert "not mapped" in v.reason


def test_report_splits_population_and_skips_inactive():
    workers = [
        worker("material_handler", "a"),
        worker("forklift_operator", "b"),
        worker("material_handler", "c", active=False),
    ]
    creds = {
        "a": [cred("safety_orientation", "a"), cred("ppe_issued", "a")],
        "b": [cred("safety_orientation", "b"), cred("ppe_issued", "b")],
        "c": [cred("safety_orientation", "c"), cred("ppe_issued", "c")],
    }
    report = build_report(workers, creds, REQUIREMENTS, TODAY)
    assert report.summary() == {"cleared": 1, "blocked": 1, "warnings": 0}
    assert report.blocked[0].worker.worker_id == "b"


# -- config ---------------------------------------------------------------


def test_every_role_alias_is_reachable_and_maps_to_a_role_with_requirements():
    aliases = CONFIG["aliases"]
    for key, target in aliases.items():
        # normalize_role looks up the folded cell text, so a key written with
        # capitals or a hyphen could never match.
        assert normalize_role(key, aliases) == target, f"alias {key!r} is not in folded form"
        assert target in REQUIREMENTS, f"alias {key!r} targets {target}, which has no requirements"


# -- persistence ----------------------------------------------------------


def test_credentials_persist_and_attach_to_the_right_worker(tmp_path):
    path = tmp_path / "roster.db"
    with Store(path) as store:
        registry = store.load_registry()
        row = RosterRow(2, normalize_name("Tomas", "Ruiz"), normalize_phone("8325550214"),
                        normalize_email("tr@example.com"), "forklift_operator")
        compute_diff(registry, [row], TODAY)
        store.save_registry(registry)
        wid = registry.workers[0].worker_id

        store.grant_credential(cred("safety_orientation", wid))
        store.grant_credential(cred("ppe_issued", wid))
        store.grant_credential(cred("forklift_certification", wid, expires=TODAY + timedelta(days=200)))

    with Store(path) as store:
        registry = store.load_registry()
        report = build_report(registry.workers, store.all_credentials(), REQUIREMENTS, TODAY)
        assert report.summary()["cleared"] == 1
        assert len(store.credentials_for(wid)) == 3


def test_granting_to_an_unknown_worker_is_refused(tmp_path):
    with Store(tmp_path / "roster.db") as store:
        with pytest.raises(ValueError):
            store.grant_credential(cred("ppe_issued", "does-not-exist"))
