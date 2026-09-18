# Contract Labour Roster Sync

Ingest, identity resolution and change detection for a contingent workforce
roster delivered as a weekly spreadsheet.

## Scenario

A construction and installation programme inside a live fulfilment centre
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
when, because deactivating somebody's site access on a judgement call is the
kind of thing that gets asked about later.

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
badged at the gate are distinguishable readings rather than one undifferen-
tiated variance. Day-level detail sits beneath the pay-period totals, so a
dispute can be named to a date instead of argued as a total.

**Identifier ownership is enforced by the database.** The primary key on
`(kind, value)` in `worker_identifiers` means a phone or email can only point
at one worker. An attempt to merge a flagged row onto a worker who does not
own its identifier is refused rather than silently splitting a person across
two records.

**Returning workers are not new hires.** A rolled-off worker who reappears
reactivates under the original identifier, preserving credential history.

**Role mapping and header spellings live in configuration.** The agency
writes `Material Handler`, `material handler` and `MH` in different weeks.
Adding a spelling is a change to `config/roles.yaml`, not to code. An
unrecognised role returns `None` and surfaces as an exception rather than
defaulting to the least-privileged role.

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
  store.py       SQLite persistence for workers, periods, queue, credentials
  review.py      confirm / reject resolution of flagged rows
  credentials.py credential records and the eligibility gate
  provisioning.py access-system adapters, OAuth client, idempotent sync
  hours.py       three-way hours reconciliation and source adapters
config/roles.yaml    role aliases and per-role credential requirements
samples/             generator for deliberately messy sample workbooks
tests/               65 tests covering normalization, matching, diffing,
                     persistence, rerun safety, review resolution, the
                     eligibility gate, provisioning and reconciliation
```

Storage sits behind `store.py` alone: `identity.py` and `diff.py` know
nothing about it, so the matching logic stays testable in memory.

## Running it

```bash
pip install openpyxl pyyaml pytest
python samples/make_samples.py        # generate messy sample workbooks
python samples/make_hours_samples.py  # generate the agency hours file
python -m pytest tests/ -q            # 65 tests
python samples/run_pipeline.py        # end-to-end walkthrough
```

`run_pipeline.py` processes two roster files, grants credentials, runs the
eligibility gate, provisions access twice to show the second run making no
calls, and reconciles a pay period across three hours sources.

## Spreadsheet defects handled

Agency files are written for humans. The parser locates the header row by
scoring the first fifteen rows against known header spellings rather than
assuming row one, and survives: a title row above the headers, blank spacer
rows, trailing notes below the data, phone numbers stored as text in three
formats and as a float by Excel, extensions appended to numbers, missing
emails, generational suffixes appearing intermittently, middle names
appearing intermittently, and `Last, First` collapsed into one cell.

## Scope boundary

Nothing from the original scope remains unbuilt. Orientation scheduling, PPE and training notifications, reminders and
escalation are deliberately out of scope for this repository. Credential
*state* belongs here; credential *scheduling and messaging* belongs in the
companion iPaaS project, where retry and multi-day reminder sequences are
solved problems rather than something to hand-roll.
