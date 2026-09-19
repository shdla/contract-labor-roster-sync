from datetime import date

import pytest

from roster_sync.credentials import Credential, build_report
from roster_sync.diff import compute_diff
from roster_sync.hours import (
    AGENCY, SCANNER, SITE, HoursRecord, read_agency_report, read_punch_log,
    read_site_feed, reconcile,
)
from roster_sync.models import RosterRow
from roster_sync.normalize import normalize_email, normalize_name, normalize_phone
from roster_sync.provisioning import (
    HttpProvisioner, InMemoryProvisioner, OAuthClientCredentials, ProvisioningError, sync_access,
)
from roster_sync.store import Store

REQ = {"material_handler": ["safety_orientation", "ppe_issued"]}
D1, D2, D3 = date(2024, 7, 15), date(2024, 7, 16), date(2024, 7, 17)


def row(first, last, phone, email, source_row=2):
    return RosterRow(source_row, normalize_name(first, last), normalize_phone(phone),
                     normalize_email(email), "material_handler")


@pytest.fixture
def populated(tmp_path):
    store = Store(tmp_path / "r.db")
    registry = store.load_registry()
    compute_diff(registry, [row("Tomas", "Ruiz", "8325550214", "tr@example.com"),
                            row("Alicia", "Fontenot", "8325550288", "af@example.com")], D1)
    store.save_registry(registry)
    ruiz = next(w for w in registry.workers if w.name.last == "ruiz")
    fontenot = next(w for w in registry.workers if w.name.last == "fontenot")
    for w in (ruiz, fontenot):
        store.grant_credential(Credential(w.worker_id, "safety_orientation", D1))
    store.grant_credential(Credential(ruiz.worker_id, "ppe_issued", D1))
    yield store, registry, ruiz, fontenot
    store.close()


# -- provisioning ---------------------------------------------------------


def test_sync_activates_cleared_records_blocked_and_is_idempotent(populated):
    store, registry, ruiz, fontenot = populated
    report = build_report(registry.workers, store.all_credentials(), REQ, D1)
    prov = InMemoryProvisioner()

    first = sync_access(report, store, prov)
    assert first.summary() == {"activated": 1, "deactivated": 1, "unchanged": 0, "failed": 0}
    assert prov.calls == [("activate", ruiz.worker_id)], "never-provisioned blocked worker needs no call"

    second = sync_access(report, store, prov)
    assert second.summary()["unchanged"] == 2
    assert len(prov.calls) == 1, "a rerun against unchanged state makes no calls"


def test_newly_blocked_worker_is_revoked_once(populated):
    store, registry, ruiz, fontenot = populated
    prov = InMemoryProvisioner()
    sync_access(build_report(registry.workers, store.all_credentials(), REQ, D1), store, prov)

    # The agency reassigns him to a role with no configured requirements,
    # so the gate can no longer clear him and access must be revoked.
    ruiz.role = "unknown_role"
    report = build_report(registry.workers, store.all_credentials(), REQ, D2)
    outcome = sync_access(report, store, prov)

    assert outcome.deactivated == [ruiz.worker_id]
    assert prov.calls[-1] == ("deactivate", ruiz.worker_id)
    assert ruiz.worker_id not in prov.active

    calls = len(prov.calls)
    again = sync_access(report, store, prov)
    assert len(prov.calls) == calls, "revoked once, not once per run"
    assert ruiz.worker_id in again.unchanged


def test_leaver_is_revoked_through_inactive_and_only_once(populated):
    store, registry, ruiz, fontenot = populated
    store.grant_credential(Credential(fontenot.worker_id, "ppe_issued", D1))
    prov = InMemoryProvisioner()
    sync_access(build_report(registry.workers, store.all_credentials(), REQ, D1), store, prov)
    state, original_ref = store.access_state(fontenot.worker_id)
    assert state == "active"

    # Two further roster periods without her; the second makes her a leaver.
    still_here = [row("Tomas", "Ruiz", "8325550214", "tr@example.com")]
    compute_diff(registry, still_here, D2)
    diff = compute_diff(registry, still_here, D3)
    assert diff.leavers == [fontenot]

    # build_report skips inactive workers, so `inactive` is the only route to her badge.
    report = build_report(registry.workers, store.all_credentials(), REQ, D3)
    outcome = sync_access(report, store, prov, inactive=diff.leavers)
    assert outcome.deactivated == [fontenot.worker_id]
    assert prov.calls[-1] == ("deactivate", fontenot.worker_id)
    assert store.access_state(fontenot.worker_id) == ("revoked", original_ref)

    calls = len(prov.calls)
    sync_access(report, store, prov, inactive=diff.leavers)
    assert len(prov.calls) == calls, "a rerun does not revoke her again"


def test_failure_on_one_worker_does_not_stop_the_others(populated):
    store, registry, ruiz, fontenot = populated
    store.grant_credential(Credential(fontenot.worker_id, "ppe_issued", D1))

    class Flaky(InMemoryProvisioner):
        def activate(self, worker):
            if worker.name.last == "ruiz":
                raise ProvisioningError("boom")
            return super().activate(worker)

    report = build_report(registry.workers, store.all_credentials(), REQ, D1)
    outcome = sync_access(report, store, Flaky())
    assert outcome.summary()["failed"] == 1
    assert outcome.summary()["activated"] == 1
    assert store.access_state(ruiz.worker_id) == (None, None), "failed push leaves no false state"


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body


class NotJson(FakeResponse):
    def json(self):
        raise ValueError("body is not JSON")


def play(script):
    """Next scripted reply; an exception instance stands for a transport error and is raised."""
    reply = script.pop(0)
    if isinstance(reply, Exception):
        raise reply
    return reply


class FakeSession:
    def __init__(self, script, token_script=()):
        self.script, self.token_script, self.requests = list(script), list(token_script), []

    def post(self, url, **kw):
        self.requests.append(("post", url, kw))
        if url.endswith("/token"):
            if self.token_script:
                return play(self.token_script)
            return FakeResponse(200, {"access_token": "tok", "expires_in": 3600})
        return play(self.script)

    def delete(self, url, **kw):
        self.requests.append(("delete", url, kw))
        return play(self.script)


def test_http_provisioner_retries_on_429_then_succeeds(populated):
    _, _, ruiz, _ = populated
    session = FakeSession([FakeResponse(429), FakeResponse(503), FakeResponse(201, {"reference": "B-1"})])
    slept = []
    prov = HttpProvisioner("https://access.example/api",
                           OAuthClientCredentials("https://access.example/token", "id", "secret"),
                           session, backoff_seconds=0.1, sleep=slept.append)
    assert prov.activate(ruiz) == "B-1"
    assert slept == [0.1, 0.2], "exponential backoff"
    grants = [r for r in session.requests if r[1].endswith("/grants")]
    assert len(grants) == 3
    # The retry is the request the key exists for, so every attempt must carry it.
    assert [g[2]["headers"].get("Idempotency-Key") for g in grants] == [ruiz.worker_id] * 3
    assert all(g[2]["headers"]["Authorization"] == "Bearer tok" for g in grants)
    assert sum(1 for r in session.requests if r[1].endswith("/token")) == 1, "token cached across retries"


def test_http_provisioner_refreshes_the_token_once_on_401(populated):
    _, _, ruiz, _ = populated
    session = FakeSession([FakeResponse(401), FakeResponse(201, {"reference": "B-1"})])
    prov = HttpProvisioner("https://access.example/api",
                           OAuthClientCredentials("https://access.example/token", "id", "secret"),
                           session, sleep=lambda _: None)
    assert prov.activate(ruiz) == "B-1"
    assert sum(1 for r in session.requests if r[1].endswith("/token")) == 2, "401 forces one refresh"
    grants = [r for r in session.requests if r[1].endswith("/grants")]
    assert len(grants) == 2
    assert grants[1][2]["headers"].get("Idempotency-Key") == ruiz.worker_id


def test_oauth_credentials_keep_the_secret_out_of_repr_and_the_cache_out_of_init():
    assert "SUPERSECRET" not in repr(OAuthClientCredentials("https://a/token", "id", "SUPERSECRET"))
    with pytest.raises(TypeError):
        OAuthClientCredentials("https://a/token", "id", "s", _token="preset")


def test_http_provisioner_gives_up_after_max_attempts(populated):
    _, _, ruiz, _ = populated
    session = FakeSession([FakeResponse(503)] * 4)
    prov = HttpProvisioner("https://a/api", OAuthClientCredentials("https://a/token", "i", "s"),
                           session, max_attempts=4, sleep=lambda _: None)
    with pytest.raises(ProvisioningError, match="after 4 attempts: status 503"):
        prov.activate(ruiz)


def http_provisioner(session, **kw):
    kw.setdefault("sleep", lambda _: None)
    return HttpProvisioner("https://access.example/api",
                           OAuthClientCredentials("https://access.example/token", "id", "secret"),
                           session, **kw)


def test_http_provisioner_retries_a_transport_error_and_keeps_the_idempotency_key(populated):
    _, _, ruiz, _ = populated
    session = FakeSession([ConnectionError("connection reset"), FakeResponse(201, {"reference": "B-1"})])
    slept = []
    assert http_provisioner(session, backoff_seconds=0.5, sleep=slept.append).activate(ruiz) == "B-1"
    assert slept == [0.5], "a transport error backs off like a 503"
    grants = [r for r in session.requests if r[1].endswith("/grants")]
    # The first post may have been applied before the connection dropped; the key is what makes the resend safe.
    assert [g[2]["headers"].get("Idempotency-Key") for g in grants] == [ruiz.worker_id] * 2


def test_http_provisioner_retries_a_transport_error_from_the_token_endpoint(populated):
    _, _, ruiz, _ = populated
    session = FakeSession([FakeResponse(201, {"reference": "B-1"})], token_script=[TimeoutError("timed out")])
    slept = []
    assert http_provisioner(session, backoff_seconds=0.5, sleep=slept.append).activate(ruiz) == "B-1"
    assert slept == [0.5]
    assert sum(1 for r in session.requests if r[1].endswith("/token")) == 2


def test_transport_failure_on_one_worker_is_recorded_and_does_not_stop_the_others(populated):
    store, registry, ruiz, fontenot = populated
    store.grant_credential(Credential(fontenot.worker_id, "ppe_issued", D1))

    class DownForRuiz(FakeSession):
        def post(self, url, **kw):
            if kw.get("json", {}).get("external_id") == ruiz.worker_id:
                self.requests.append(("post", url, kw))
                raise ConnectionError("connection reset")
            return super().post(url, **kw)

    session = DownForRuiz([FakeResponse(201, {"reference": "B-2"})])
    slept = []
    report = build_report(registry.workers, store.all_credentials(), REQ, D1)
    outcome = sync_access(report, store, http_provisioner(session, max_attempts=4, backoff_seconds=0.5,
                                                          sleep=slept.append))

    assert [wid for wid, _ in outcome.failed] == [ruiz.worker_id]
    assert "after 4 attempts: ConnectionError" in outcome.failed[0][1]
    assert slept == [0.5, 1.0, 2.0], "retried with backoff, and no sleep after the last attempt"
    assert outcome.activated == [fontenot.worker_id], "the worker after the failure is still attempted"
    assert store.access_state(ruiz.worker_id) == (None, None), "failed push leaves no false state"
    assert store.access_state(fontenot.worker_id) == ("active", "B-2")


def test_a_401_with_no_attempt_left_to_refresh_on_is_a_provisioning_error(populated):
    _, _, ruiz, _ = populated
    with pytest.raises(ProvisioningError, match="after 1 attempts: status 401"):
        http_provisioner(FakeSession([FakeResponse(401)]), max_attempts=1).activate(ruiz)


@pytest.mark.parametrize("token_reply", [FakeResponse(200, {"token_type": "bearer"}), NotJson(200)])
def test_token_reply_without_an_access_token_is_a_provisioning_error(populated, token_reply):
    _, _, ruiz, _ = populated
    with pytest.raises(ProvisioningError, match="access_token"):
        http_provisioner(FakeSession([], token_script=[token_reply])).activate(ruiz)


@pytest.mark.parametrize("grant_reply", [FakeResponse(201, {"id": 7}), NotJson(201)])
def test_grant_reply_without_a_reference_is_a_provisioning_error(populated, grant_reply):
    _, _, ruiz, _ = populated
    with pytest.raises(ProvisioningError, match="reference"):
        http_provisioner(FakeSession([grant_reply])).activate(ruiz)


def test_http_deactivate_deletes_by_external_ref_and_accepts_404(populated):
    _, _, ruiz, _ = populated
    session = FakeSession([FakeResponse(204), FakeResponse(404)])
    prov = http_provisioner(session)
    prov.deactivate(ruiz, "B-1")
    prov.deactivate(ruiz, "B-1")  # already gone at the access system: the wanted state, not an error

    deletes = [r for r in session.requests if r[0] == "delete"]
    assert [d[1] for d in deletes] == ["https://access.example/api/access/grants/B-1"] * 2
    assert [d[2]["headers"].get("Idempotency-Key") for d in deletes] == [ruiz.worker_id] * 2


def test_http_deactivate_without_an_external_ref_makes_no_request(populated):
    _, _, ruiz, _ = populated
    session = FakeSession([])
    http_provisioner(session).deactivate(ruiz, None)
    assert session.requests == [], "nothing was granted, so not even a token is fetched"


def test_http_deactivate_gives_up_after_max_attempts(populated):
    _, _, ruiz, _ = populated
    session = FakeSession([FakeResponse(500)] * 4)
    with pytest.raises(ProvisioningError):
        http_provisioner(session, max_attempts=4).deactivate(ruiz, "B-1")
    assert sum(1 for r in session.requests if r[0] == "delete") == 4


# -- hours ----------------------------------------------------------------


def rec(source, wid, day, hours):
    return HoursRecord(source, wid, day, hours)


def test_three_way_readings():
    agency = [rec(AGENCY, "w", D1, 8), rec(AGENCY, "w", D2, 8), rec(AGENCY, "w", D3, 8)]
    scanner = [rec(SCANNER, "w", D1, 8), rec(SCANNER, "w", D2, 6), rec(SCANNER, "w", D3, 0)]
    site = [rec(SITE, "w", D1, 8), rec(SITE, "w", D2, 6), rec(SITE, "w", D3, 0)]
    report = reconcile(agency, scanner, site, D1, D3)

    days = {d.day: d.reading for d in report.workers[0].days}
    assert days[D1] == "clean"
    assert days[D2] == "agency over-reported"
    assert days[D3] == "claimed but unrecorded"
    assert report.summary()["over_reported_hours"] == 10
    assert not report.workers[0].clean


def test_present_but_not_badged_at_site_is_distinguished():
    report = reconcile([rec(AGENCY, "w", D1, 8)], [rec(SCANNER, "w", D1, 8)], [rec(SITE, "w", D1, 0)], D1, D1)
    assert report.workers[0].days[0].reading == "present but not badged at site"


def test_without_a_site_feed_two_way_still_works():
    report = reconcile([rec(AGENCY, "w", D1, 8)], [rec(SCANNER, "w", D1, 7)], None, D1, D1)
    assert report.workers[0].site is None
    assert report.workers[0].days[0].reading == "agency over-reported"


@pytest.mark.parametrize("agency,scanner,site,reading", [
    (6, 8, 8, "agency under-reported"),
    (8, 6, 4, "mixed variance"),
])
def test_three_way_under_reported_and_mixed_variance(agency, scanner, site, reading):
    report = reconcile([rec(AGENCY, "w", D1, agency)], [rec(SCANNER, "w", D1, scanner)],
                       [rec(SITE, "w", D1, site)], D1, D1)
    assert report.workers[0].days[0].reading == reading


def no_out_punch(source):
    return HoursRecord(source, "w", D1, 0.0, "missing out-punch")


@pytest.mark.parametrize("scanner,site", [
    ([no_out_punch(SCANNER)], None),                     # classified alone: agency over-reported
    ([no_out_punch(SCANNER)], [no_out_punch(SITE)]),     # classified alone: claimed but unrecorded
    ([rec(SCANNER, "w", D1, 8)], [no_out_punch(SITE)]),  # classified alone: present but not badged at site
])
def test_in_punch_without_an_out_punch_is_incomplete_not_a_variance(scanner, site):
    # The row proves the worker punched in; its 0.0 hours are unknown, not evidence against the agency.
    # D2 is complete in every source, so it must still be classified.
    agency = [rec(AGENCY, "w", D1, 8), rec(AGENCY, "w", D2, 8)]
    scanner = scanner + [rec(SCANNER, "w", D2, 8)]
    site = None if site is None else site + [rec(SITE, "w", D2, 8)]
    report = reconcile(agency, scanner, site, D1, D2)
    days = {d.day: d.reading for d in report.workers[0].days}
    assert days == {D1: "incomplete punch; verify before disputing", D2: "clean"}, "only the noted day is held back"
    assert report.disputed == report.workers, "not clean, so it still reaches a person"


def test_split_shift_punches_sum_and_days_outside_the_period_are_excluded():
    agency = [rec(AGENCY, "w", D1, 8), rec(AGENCY, "w", D2, 8), rec(AGENCY, "w", D3, 8)]
    # Out for lunch and back in is two punch pairs on one day.
    scanner = [rec(SCANNER, "w", D2, 4), rec(SCANNER, "w", D2, 4)]
    worker = reconcile(agency, scanner, None, D2, D2).workers[0]

    assert [(d.day, d.agency, d.scanner, d.reading) for d in worker.days] == [(D2, 8, 8, "clean")]
    assert (worker.agency, worker.scanner) == (8, 8), "D1 and D3 are outside the period"


def test_tolerance_absorbs_rounding():
    report = reconcile([rec(AGENCY, "w", D1, 8.0)], [rec(SCANNER, "w", D1, 7.8)], None, D1, D1)
    assert report.workers[0].clean


@pytest.mark.parametrize("scanner,clean", [(7.75, True), (7.74, False)])
def test_tolerance_boundary_is_inclusive(scanner, clean):
    report = reconcile([rec(AGENCY, "w", D1, 8.0)], [rec(SCANNER, "w", D1, scanner)], None, D1, D1)
    assert report.workers[0].clean is clean


def test_agency_rows_resolve_through_registry_and_unknowns_are_not_guessed(tmp_path, populated):
    store, registry, ruiz, _ = populated
    csv_path = tmp_path / "agency.csv"
    csv_path.write_text(
        "First Name,Last Name,Phone,Email,Date,Hours\n"
        "Tomas,Ruiz,(832) 555-0214,tr@example.com,2024-07-15,8\n"
        "Tomas,Ruiz,(832) 555-0214,tr@example.com,07/16/2024,8.5\n"
        "Somebody,New,832-555-0999,,2024-07-15,8\n"
    )
    records, unresolved = read_agency_report(csv_path, registry)
    assert [r.worker_id for r in records] == [ruiz.worker_id] * 2
    assert records[1].hours == 8.5
    assert len(unresolved) == 1 and "new" in unresolved[0].reason


def test_agency_row_matching_on_name_alone_is_given_no_hours(tmp_path, populated):
    _, registry, _, _ = populated
    csv_path = tmp_path / "agency.csv"
    csv_path.write_text(
        "First Name,Last Name,Phone,Email,Date,Hours\n"
        "Tomas,Ruiz,832-555-0999,,2024-07-15,8\n"
    )
    # Same rule as the roster: a name is not enough to attribute invoice hours to a person.
    records, unresolved = read_agency_report(csv_path, registry)
    assert records == []
    assert [(u.line, u.reason) for u in unresolved] == [(2, "identity weak_name")]


def test_short_agency_row_is_unresolved_and_the_rest_of_the_file_is_read(tmp_path, populated):
    _, registry, ruiz, _ = populated
    csv_path = tmp_path / "agency.csv"
    csv_path.write_text(
        "First Name,Last Name,Phone,Email,Date,Hours\n"
        "Tomas,Ruiz,(832) 555-0214,tr@example.com,2024-07-15,8\n"
        "Tomas,Ruiz,(832) 555-0214,tr@example.com,2024-07-16\n"
        "Tomas,Ruiz,(832) 555-0214,tr@example.com,2024-07-17,8.5\n"
    )
    # csv.DictReader hands a truncated line None for the missing cells; one such line must not abort the file.
    records, unresolved = read_agency_report(csv_path, registry)
    assert [(r.worker_id, r.day, r.hours) for r in records] == [(ruiz.worker_id, D1, 8.0), (ruiz.worker_id, D3, 8.5)]
    assert [(u.source, u.line) for u in unresolved] == [(AGENCY, 3)]
    assert "float" in unresolved[0].reason


def test_punch_log_computes_hours_and_flags_missing_out(tmp_path):
    p = tmp_path / "scan.csv"
    p.write_text("worker_id,date,in,out\nw,2024-07-15,06:00,14:30\nw,2024-07-16,06:00,\nw,2024-07-17,22:00,06:00\n")
    records, unresolved = read_punch_log(p, SCANNER)
    assert [r.hours for r in records] == [8.5, 0.0, 8.0]
    assert records[1].note == "missing out-punch"
    assert unresolved == []


def test_short_punch_row_is_unresolved_and_the_rest_of_the_file_is_read(tmp_path):
    p = tmp_path / "scan.csv"
    p.write_text("worker_id,date,in,out\nw,2024-07-15,06:00,14:30\nw\nw,2024-07-16,06:00\nw,2024-07-17,06:00,14:00\n")
    records, unresolved = read_punch_log(p, SCANNER)
    # A line cut before the date cannot be placed on a day; a line cut before "out" is an empty out cell.
    assert [(r.day, r.hours, r.note) for r in records] == [
        (D1, 8.5, ""), (D2, 0.0, "missing out-punch"), (D3, 8.0, "")]
    assert [(u.source, u.line) for u in unresolved] == [(SCANNER, 3)]
    assert "date" in unresolved[0].reason


def test_overnight_shift_on_the_last_day_of_a_month(tmp_path):
    p = tmp_path / "scan.csv"
    p.write_text("worker_id,date,in,out\nw,2024-07-31,22:00,06:00\n")
    records, unresolved = read_punch_log(p, SCANNER)
    assert unresolved == [], "a month boundary must not break the rollover"
    assert records[0].hours == 8.0


def test_site_feed_uses_badge_map_and_reports_unmapped_badges(tmp_path, populated):
    store, registry, ruiz, _ = populated
    store.map_badge("BADGE-0042", ruiz.worker_id, D1)
    p = tmp_path / "site.csv"
    p.write_text("badge_id,date,in,out\nBADGE-0042,2024-07-15,05:52,14:35\nBADGE-9999,2024-07-15,06:00,14:00\n")
    records, unresolved = read_site_feed(p, store.badge_map())
    assert len(records) == 1 and records[0].worker_id == ruiz.worker_id
    assert len(unresolved) == 1 and "BADGE-9999" in unresolved[0].reason
