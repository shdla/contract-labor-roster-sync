"""Provisioning site access from eligibility verdicts.

The gate decides who is cleared; this module makes the access system agree
with it, and does nothing when it already does. Every call is idempotent by
construction: the last state pushed for each worker is recorded, and a
worker whose state has not changed generates no request. A nightly run
against an unchanged population makes zero calls.

The access system is reached through a Provisioner adapter. The HTTP
implementation authenticates with OAuth 2.0 client credentials, caches the
token until shortly before expiry, retries on 429 and 5xx with exponential
backoff, and sends the worker id as an idempotency key so a retried request
cannot create a duplicate badge. An in-memory implementation exists for
tests and demos, and for the case where the customer's security team has
not yet approved API access — the sync logic is identical either way.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from .credentials import EligibilityReport
from .models import Worker

log = logging.getLogger(__name__)


class ProvisioningError(RuntimeError):
    pass


class Provisioner(Protocol):
    def activate(self, worker: Worker) -> str:
        """Grant access. Returns the access system's reference for the worker."""

    def deactivate(self, worker: Worker, external_ref: str | None) -> None:
        """Revoke access."""


# -- in-memory --------------------------------------------------------------


class InMemoryProvisioner:
    """Records calls instead of making them. Used in tests and demos."""

    def __init__(self) -> None:
        self.active: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def activate(self, worker: Worker) -> str:
        ref = f"BADGE-{worker.worker_id[:8].upper()}"
        self.active[worker.worker_id] = ref
        self.calls.append(("activate", worker.worker_id))
        return ref

    def deactivate(self, worker: Worker, external_ref: str | None) -> None:
        self.active.pop(worker.worker_id, None)
        self.calls.append(("deactivate", worker.worker_id))


# -- http -------------------------------------------------------------------


@dataclass
class OAuthClientCredentials:
    token_url: str
    client_id: str
    client_secret: str
    scope: str = ""
    refresh_margin_seconds: int = 60
    _token: str | None = field(default=None, repr=False)
    _expires_at: float = 0.0

    def token(self, session) -> str:
        if self._token and time.time() < self._expires_at - self.refresh_margin_seconds:
            return self._token
        payload = {"grant_type": "client_credentials", "client_id": self.client_id,
                   "client_secret": self.client_secret}
        if self.scope:
            payload["scope"] = self.scope
        response = session.post(self.token_url, data=payload, timeout=15)
        if response.status_code != 200:
            raise ProvisioningError(f"token endpoint returned {response.status_code}")
        body = response.json()
        self._token = body["access_token"]
        self._expires_at = time.time() + int(body.get("expires_in", 3600))
        return self._token


class HttpProvisioner:
    """Provisioner over a REST access API.

    session is any object with .post/.delete returning objects that expose
    .status_code and .json(); requests.Session satisfies this, and tests
    inject a fake.
    """

    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self, base_url: str, credentials: OAuthClientCredentials, session,
                 max_attempts: int = 4, backoff_seconds: float = 0.5, sleep=time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self.credentials = credentials
        self.session = session
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        last = None
        # Popped once: a pop inside the loop would strip the caller's headers from every retry.
        extra = kwargs.pop("headers", {})
        for attempt in range(1, self.max_attempts + 1):
            # Authorization is rebuilt per attempt so a 401 refresh takes effect.
            headers = {"Authorization": f"Bearer {self.credentials.token(self.session)}", **extra}
            response = getattr(self.session, method)(url, headers=headers, timeout=15, **kwargs)
            if response.status_code == 401 and attempt == 1:
                self.credentials._token = None  # force refresh once, then treat as failure
                continue
            if response.status_code in self.RETRY_STATUSES and attempt < self.max_attempts:
                delay = self.backoff_seconds * (2 ** (attempt - 1))
                log.warning("access api %s %s -> %s; retry %d in %.1fs",
                            method.upper(), path, response.status_code, attempt, delay)
                self._sleep(delay)
                last = response
                continue
            return response
        raise ProvisioningError(f"{method.upper()} {path} failed after {self.max_attempts} attempts"
                                f" (last status {last.status_code if last else 'n/a'})")

    def activate(self, worker: Worker) -> str:
        body = {"external_id": worker.worker_id, "display_name": worker.name.display,
                "role": worker.role}
        response = self._request("post", "/access/grants", json=body,
                                 headers={"Idempotency-Key": worker.worker_id})
        if response.status_code not in (200, 201):
            raise ProvisioningError(f"activate {worker.worker_id}: {response.status_code}")
        return response.json()["reference"]

    def deactivate(self, worker: Worker, external_ref: str | None) -> None:
        if not external_ref:
            log.info("no external reference for %s; nothing to revoke", worker.worker_id)
            return
        response = self._request("delete", f"/access/grants/{external_ref}",
                                 headers={"Idempotency-Key": worker.worker_id})
        if response.status_code not in (200, 202, 204, 404):
            raise ProvisioningError(f"deactivate {external_ref}: {response.status_code}")


# -- sync -------------------------------------------------------------------


@dataclass
class SyncOutcome:
    as_of: date
    activated: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {"activated": len(self.activated), "deactivated": len(self.deactivated),
                "unchanged": len(self.unchanged), "failed": len(self.failed)}


def sync_access(report: EligibilityReport, store, provisioner: Provisioner,
                inactive: list[Worker] = ()) -> SyncOutcome:
    """Push the report's verdicts to the access system, touching only changes.

    Cleared workers become active. Blocked workers and inactive (rolled-off)
    workers become revoked. State is read from and written to the store so a
    rerun makes no calls. A failure on one worker is recorded and does not
    stop the others — partial progress is better than none, and the failed
    list is the retry queue.
    """
    outcome = SyncOutcome(as_of=report.as_of)
    desired: list[tuple[Worker, str]] = (
        [(v.worker, "active") for v in report.cleared]
        + [(v.worker, "revoked") for v in report.blocked]
        + [(w, "revoked") for w in inactive]
    )

    for worker, wanted in desired:
        current_state, external_ref = store.access_state(worker.worker_id)
        if current_state == wanted:
            outcome.unchanged.append(worker.worker_id)
            continue
        try:
            if wanted == "active":
                ref = provisioner.activate(worker)
                store.set_access_state(worker.worker_id, "active", ref)
                outcome.activated.append(worker.worker_id)
            else:
                # A never-provisioned worker being blocked needs no call,
                # only a recorded state so the next run stays quiet.
                if current_state is not None:
                    provisioner.deactivate(worker, external_ref)
                store.set_access_state(worker.worker_id, "revoked", external_ref)
                outcome.deactivated.append(worker.worker_id)
        except ProvisioningError as exc:
            log.error("provisioning %s -> %s failed: %s", worker.worker_id, wanted, exc)
            outcome.failed.append((worker.worker_id, str(exc)))
    return outcome
