"""Credential state and the eligibility gate.

The gate answers one question every evening: who is cleared to work tomorrow,
for what role, and who is blocked and why. It is derived from three things —
the worker's role, the credentials that role requires (config), and the
credentials the worker actually holds (records with optional expiry).

Rules:

- A missing required credential blocks.
- An expired required credential blocks.
- A credential granted after the evaluation date is not held yet, so it
  reads as missing. An orientation booked for Thursday clears nobody on
  Tuesday.
- A credential expiring within warn_days clears the worker but raises a
  warning, so renewals are scheduled before they become a block.
- A worker whose role could not be mapped is blocked, never defaulted to
  the least-demanding role. An unmapped role means we do not know what
  they need, and guessing in the permissive direction is how somebody ends
  up on a forklift without a certificate.
- Inactive workers are excluded entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from .models import Worker


@dataclass(frozen=True)
class Credential:
    worker_id: str
    kind: str
    granted_on: date
    expires_on: date | None = None
    source: str = ""
    reference: str = ""

    def status_on(self, as_of: date, warn_days: int) -> "CredentialStatus":
        if self.expires_on is None:
            return CredentialStatus.VALID
        if self.expires_on < as_of:
            return CredentialStatus.EXPIRED
        if self.expires_on <= as_of + timedelta(days=warn_days):
            return CredentialStatus.EXPIRING
        return CredentialStatus.VALID


class CredentialStatus(str, Enum):
    VALID = "valid"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    MISSING = "missing"


@dataclass
class Finding:
    kind: str
    status: CredentialStatus
    expires_on: date | None = None

    @property
    def blocks(self) -> bool:
        return self.status in (CredentialStatus.MISSING, CredentialStatus.EXPIRED)


@dataclass
class Verdict:
    worker: Worker
    cleared: bool
    findings: list[Finding] = field(default_factory=list)
    reason: str = ""

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocks]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.status is CredentialStatus.EXPIRING]

    def describe(self) -> str:
        name = self.worker.name.display
        if self.reason:
            return f"BLOCKED  {name:24} {self.reason}"
        if not self.cleared:
            items = ", ".join(f"{f.kind} {f.status.value}" for f in self.blocking)
            return f"BLOCKED  {name:24} {items}"
        if self.warnings:
            items = ", ".join(f"{f.kind} expires {f.expires_on}" for f in self.warnings)
            return f"CLEARED  {name:24} warning: {items}"
        return f"CLEARED  {name:24}"


@dataclass
class EligibilityReport:
    as_of: date
    cleared: list[Verdict] = field(default_factory=list)
    blocked: list[Verdict] = field(default_factory=list)

    @property
    def warned(self) -> list[Verdict]:
        return [v for v in self.cleared if v.warnings]

    def summary(self) -> dict[str, int]:
        return {"cleared": len(self.cleared), "blocked": len(self.blocked), "warnings": len(self.warned)}


def evaluate(
    worker: Worker,
    held: list[Credential],
    requirements: dict[str, list[str]],
    as_of: date,
    warn_days: int = 14,
) -> Verdict:
    """Decide whether one worker is cleared on as_of."""
    if worker.role is None:
        return Verdict(worker=worker, cleared=False, reason="role not mapped; requirements unknown")
    required = requirements.get(worker.role)
    if required is None:
        return Verdict(worker=worker, cleared=False, reason=f"no requirements configured for role {worker.role}")

    # Where a worker holds several records of one kind, the latest expiry
    # wins: a renewed certificate supersedes the lapsed one it replaced.
    best: dict[str, Credential] = {}
    for credential in held:
        # Not in effect yet, so not held: the gate does not know the
        # requirement is met today, and a future renewal must not displace
        # the certificate that is valid now.
        if credential.granted_on > as_of:
            continue
        current = best.get(credential.kind)
        if current is None or (credential.expires_on or date.max) > (current.expires_on or date.max):
            best[credential.kind] = credential

    findings: list[Finding] = []
    for kind in required:
        credential = best.get(kind)
        if credential is None:
            findings.append(Finding(kind=kind, status=CredentialStatus.MISSING))
            continue
        findings.append(
            Finding(kind=kind, status=credential.status_on(as_of, warn_days), expires_on=credential.expires_on)
        )

    cleared = not any(f.blocks for f in findings)
    return Verdict(worker=worker, cleared=cleared, findings=findings)


def build_report(
    workers: list[Worker],
    credentials_by_worker: dict[str, list[Credential]],
    requirements: dict[str, list[str]],
    as_of: date,
    warn_days: int = 14,
) -> EligibilityReport:
    report = EligibilityReport(as_of=as_of)
    for worker in workers:
        if not worker.active:
            continue
        verdict = evaluate(worker, credentials_by_worker.get(worker.worker_id, []), requirements, as_of, warn_days)
        (report.cleared if verdict.cleared else report.blocked).append(verdict)
    return report
