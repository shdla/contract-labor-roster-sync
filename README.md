# Contract Labor Roster Sync

Onboarding, credentialing, site-access provisioning and payroll
reconciliation for a contingent workforce whose only system of record is a
weekly spreadsheet.

Python · SQLite · OAuth 2.0 client credentials · REST · HMAC-signed webhooks ·
idempotent sync · three-way data reconciliation · 132 tests

## Scenario

A construction and installation program inside a live fulfillment center
took on an additional scope requiring roughly eighty material handlers and
pod production workers, sourced through a single staffing agency. The agency
delivered a weekly Excel roster containing five columns: first name, last
name, phone number, email and role.

Everything downstream depended on that file. Each worker had to be scheduled
for safety orientation, issued PPE, badged for site access, and — for
forklift and buckhoist roles — verified against a certification. Workers
rolled on and off continuously. Communication ran by phone, because this
population has no corporate email.

In practice the file was read by a human, the changes were worked out by
eye, and the population was tracked in a second spreadsheet.

## The problem this solves

**The roster carries no stable identifier.** No agency worker ID, no
employee number. Given Monday's file and last Monday's file, there is no
supplied key that says whether a given line is the same person as before.

That single gap governs everything else. Provisioning has to be idempotent,
so re-running a file must not create duplicate workers. Leaver detection
drives badge deactivation, so a missed match leaves site access open for
somebody who has gone. Joiner detection drives orientation scheduling, so a
false positive sends somebody to a session they have already attended.

Identity resolution without a stable source key is the same problem any
HRIS-to-directory integration faces when the upstream system is a file feed
rather than an API.

## Design decisions

**Identifiers are issued locally, never derived.** Each worker receives a
UUID on first sighting. Deriving an identifier from a name or phone hash
would mean the identifier changes whenever the source value changes, which
defeats the purpose. Observed phones and emails accumulate as aliases, so a
worker stays matchable after a phone change.

**Matching is a cascade, and only strong signals merge automatically.**
Phone match, then email match, then name. A name-only match is returned as
`WEAK_NAME` and routed to human review rather than applied. Merging two
people is a materially worse outcome than carrying a duplicate for a day.

**Conflicting signals escalate rather than resolve by precedence.** When a
phone matches one worker and the name matches another — a reassigned mobile
number, or a data entry error — no rule silently picks a winner. The row
goes to review and nothing is provisioned against an uncertain identity.

**Absence has a grace window, and it is derived rather than counted.** A
worker missing from one file is not treated as departed; a truncated export
or a dropped row would otherwise deactivate a badge for somebody still
working. Absence must persist across `absence_threshold` roster periods,
default two. It is computed from the set of periods processed, not from a
counter incremented per run — a counter would age every absent worker again
on a retried job, so a transient failure followed by a rerun could
deactivate site access for people still working. Joiners, by contrast, are
acted on immediately: onboarding somebody a day early costs far less than
somebody arriving unable to work.

**An unresolved flag never deactivates anyone.** A row waiting for human
review still counts its candidate worker as seen, so a question nobody has
answered yet cannot quietly age somebody into a leaver.

**A review decision changes the data, not a status column.** Confirming that
a flagged row belongs to an existing worker attaches the new phone or email
to that worker permanently, so the same row matches on a strong signal next
week and never reaches the queue again. Rejecting creates a second worker
deliberately, with the same effect. A queue that cannot be cleared is an
alert, and people stop reading alerts. Decisions record who made them and
when, because deactivating somebody's site access on a judgment call is the
kind of thing that gets asked about later. A decided flag cannot be decided
again, so that record is never overwritten.

**The eligibility gate blocks on uncertainty.** A worker whose role could
not be mapped is blocked, never defaulted to the least-demanding role. An
unmapped role means the requirements are unknown, and guessing in the
permissive direction is how somebody ends up on a forklift without a
certificate. Missing or expired credentials block; a credential inside the
warning window clears the worker but is reported, so renewals get scheduled
before they become a block. Credential records are append-only — a renewal
is a new row, and the gate takes the latest expiry per kind — so the history
of what somebody held and when is never overwritten.

**Provisioning is idempotent by state comparison, not by hope.** The last
state pushed for each worker is recorded, so a nightly run against an
unchanged population makes zero API calls. Requests carry the worker id as
an idempotency key, so a retry cannot create a duplicate badge. A failure on
one worker is recorded and does not stop the rest — partial progress beats
none, and the failed list is the retry queue. A push that fails records no
state, so the next run tries again rather than believing a lie.

**The access system is reached through an adapter.** An HTTP implementation
authenticates with OAuth 2.0 client credentials, caches the token until
shortly before expiry, and retries 429 and 5xx with exponential backoff. An
in-memory implementation covers tests, demos, and the real case where the
customer's security team has not approved API access yet. The sync logic is
identical either way: design for the access the customer will actually
grant, and swap the adapter when better access lands.

**Hours are reconciled three ways, not two.** Two sources show that the
numbers disagree; three show which one is wrong. Agency over-reporting,
hours claimed that nobody recorded, and presence at the work area that never
badged at the gate are distinguishable readings rather than one undifferentiated
variance. Day-level detail sits beneath the pay-period totals, so a
dispute can be named to a date instead of argued as a total.

**Identifier ownership is enforced by the database.** The primary key on
`(kind, value)` in `worker_identifiers` means a phone or email can only point
at one worker. An attempt to merge a flagged row onto a worker who does not
own its identifier is refused rather than silently splitting a person across
two records.

**Returning workers are not new hires.** A rolled-off worker who reappears
reactivates under the original identifier, preserving credential history.

**Role mapping lives in configuration.** The agency writes `Material
Handler`, `material handler` and `MH` in different weeks. Adding a spelling
is a change to `config/roles.yaml`, not to code. Header spellings are one
table in `ingest.py` (`DEFAULT_HEADER_ALIASES`), overridable per call through
`read_roster`'s `header_aliases` argument. An unrecognized role returns
`None` rather than defaulting to the least-privileged role, and nothing is
raised: a new worker with no mapped role is blocked by the gate, and an
existing worker keeps the last mapped role.

**Normalization refuses rather than guesses.** A phone of the wrong length,
a value of `n/a`, a string that is not email-shaped — all become `None`. A
wrong normalization silently merges two people; an absent value does not.

## Layout

```
roster_sync/
  normalize.py   phone, email, name and role normalization
  models.py      RosterRow, Worker, MatchResult, RosterDiff
  ingest.py      Excel parsing with header detection and column mapping
  identity.py    WorkerRegistry and the matching cascade
  diff.py        joiners, leavers, changes, review queue
  store.py       SQLite persistence: workers, periods, review queue,
                 credentials, access state, badge map
  review.py      confirm / reject resolution of flagged rows
  credentials.py credential records and the eligibility gate
  provisioning.py access-system adapters, OAuth client, idempotent sync
  hours.py       three-way hours reconciliation and source adapters
  events.py      signed, deduplicable webhook emission to the iPaaS
config/roles.yaml    role aliases and per-role credential requirements
samples/             sample-data generators, an end-to-end demo, and
                     send_test_event.py for signed webhook test events
tests/               132 tests covering normalization, matching, diffing,
                     persistence, rerun safety, review resolution, the
                     eligibility gate, provisioning, reconciliation and
                     event emission
```

Storage sits behind `store.py` alone: `identity.py` and `diff.py` know
nothing about it, so the matching logic stays testable in memory.

## Running it

```bash
pip install -r requirements.txt        # Python 3.9 or newer
python -m pytest tests/ -q            # 132 tests
python samples/run_pipeline.py        # end-to-end walkthrough
python samples/send_test_event.py --worker 2 --dry-run   # print a signed event, send nothing

# Optional: sample files are committed; rerun only after editing the generators.
python samples/make_samples.py        # generate messy sample workbooks
python samples/make_hours_samples.py  # generate the agency hours file
```

`run_pipeline.py` processes two roster files, grants credentials, runs the
eligibility gate, provisions access twice to show the second run making no
calls, and reconciles a pay period across three hours sources.

Without `--dry-run`, `send_test_event.py` posts to `ROSTER_WEBHOOK_URL` and
signs with `ROSTER_SIGNING_SECRET` (default `dev-secret`). The HTTP adapters
take an injected session (`requests.Session` or any object with `.post` and
`.delete`), so `requests` is deliberately not a dependency.

## Sample run

A name typo in week two matches on phone and updates the existing record
rather than creating a duplicate worker. The eligibility gate blocks one
worker with the reason stated. Provisioning makes seven API calls on the
first run and none on the rerun. The reconciliation distinguishes hours the
agency over-reported from hours worked at the work area that were never
badged at the gate.

Output of `python samples/run_pipeline.py`:

```
====================================================================
1. Roster ingest and identity resolution
====================================================================

roster_week1.xlsx  header row 3  7 rows  rerun=False
  {'joiners': 6, 'leavers': 0, 'changed': 0, 'unchanged': 0, 'review': 0, 'rejected': 1}
    JOINER  Marcus Webb            material_handler
    JOINER  Danielle Okonkwo       pod_production
    JOINER  Ray Villanueva         forklift_operator
    JOINER  Tomas Ruiz             material_handler
    JOINER  Priya Raghunathan      buckhoist_operator
    JOINER  Curtis Delaney         material_handler

roster_week2.xlsx  header row 3  8 rows  rerun=False
  {'joiners': 2, 'leavers': 0, 'changed': 3, 'unchanged': 2, 'review': 0, 'rejected': 1}
    JOINER  Alicia Fontenot        pod_production
    JOINER  Jerome Baptiste        forklift_operator
    CHANGED Marcuss Webb           name Marcus Webb -> Marcuss Webb
    CHANGED Danielle Okonkwo       phone added +18325550301
    CHANGED Curtis Delaney         phone added +18325550266

====================================================================
2. Credentials granted
====================================================================
  17 credential records

====================================================================
3. Eligibility gate
====================================================================
  {'cleared': 7, 'blocked': 1, 'warnings': 1}

  BLOCKED  Jerome Baptiste          ppe_issued missing, forklift_certification missing
  CLEARED  Priya Raghunathan        warning: buckhoist_training expires 2024-07-24

====================================================================
4. Access provisioning
====================================================================
  first run:  {'activated': 7, 'deactivated': 1, 'unchanged': 0, 'failed': 0}   api calls: 7
  rerun:      {'activated': 0, 'deactivated': 0, 'unchanged': 8, 'failed': 0}   api calls: 0

====================================================================
5. Three-way hours reconciliation
====================================================================
  {'workers': 2, 'clean': 1, 'disputed': 1, 'agency_hours': 32.0, 'scanner_hours': 30.0, 'over_reported_hours': 2.0, 'unresolved_rows': 1}

  Alicia Fontenot        agency 16.00  scanner 14.00  site  6.03
      2024-07-15  present but not badged at site  (agency 8.0, scanner 8.0, site 0.0)
      2024-07-16  agency over-reported  (agency 8.0, scanner 6.0, site 6.03)
  Tomas Ruiz             agency 16.00  scanner 16.00  site 16.34
  UNRESOLVED [site] badge BADGE-UNKNOWN not mapped
```

The single `deactivated` on the first run is a blocked worker who was never
provisioned, so the state is recorded without a request, which is why eight
transitions make seven calls.

## Spreadsheet defects handled

Agency files are written for humans. The parser locates the header row by
scoring the first fifteen rows against known header spellings rather than
assuming row one, and survives: a title row above the headers, blank spacer
rows, trailing notes below the data, phone numbers stored as text in three
formats and as a float by Excel, extensions appended to numbers, missing
emails, generational suffixes appearing intermittently, middle names
appearing intermittently, and `Last, First` collapsed into one cell.

## Scope boundary

The records side of the original scope is complete here; scheduling and
messaging are built in the companion Workato project. Orientation
scheduling, PPE and training notifications, reminders and escalation are
deliberately out of scope for this repository. Credential *state* belongs
here; credential *scheduling and messaging* belongs in the companion iPaaS
project (Workato), where retry and multi-day reminder sequences are solved
problems rather than something to hand-roll.

`events.py` is the boundary itself. A joiner detected in this repository
becomes a `worker.joined` webhook delivery: HMAC-SHA256-signed, and carrying
a dedup id that is a UUIDv5 of `(event type, worker id, roster period)`
rather than a random value. A retry can double-send, so delivery is
at-least-once within a run, and every retry carries the same id instead of
minting a new one. A rerun of an already-processed period does not re-emit
today: it reports no joiners, so an event that exhausted its retries is
listed in `EmitOutcome.failed` and is not sent again. The receiving recipe
reacts to that id being new or repeated; nothing about *how* it reacts
(which lookup table, what the notification says, where the error monitor
wraps) is decided in Python. That split is deliberate: an agency roster is
miserable to parse and reconcile as recipe steps, and notification
retry/escalation sequencing is a solved problem in an iPaaS that would be
tedious to hand-roll here.

### Event contract

One `worker.joined` delivery, exactly as sent: JSON with sorted keys and
compact separators.

```json
{"data":{"emails":["truiz@example.com"],"name":"Tomas Ruiz","phones":["+18325550214"],"role":"material_handler"},"event_id":"41f636b2-74cb-5539-8770-61aa82c5d470","occurred_on":"2024-07-08","subject":"22222222-1111-4111-8111-111111111111","type":"worker.joined"}
```

- Envelope keys: `event_id`, `type`, `subject` (the worker id), `occurred_on`
  (the roster period), `data`. Data keys: `name`, `role`, `phones`, `emails`.
- `X-Dedup-Id`: the event id, the same value as `event_id` in the body.
- `X-Signature-256`: lowercase hex HMAC-SHA256 of the exact body bytes, with
  no `sha256=` prefix.
- A 429, 500, 502, 503 or 504 is retried, up to four attempts in total, with
  the same id each time, so the receiver must deduplicate on `X-Dedup-Id`.
