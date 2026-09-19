"""Contract labor roster sync: ingest, identity resolution, diffing, persistence,
review, eligibility, provisioning, hours reconciliation, event emission."""

from .credentials import Credential, CredentialStatus, EligibilityReport, Verdict, build_report, evaluate
from .diff import compute_diff
from .events import (
    DeliveryError, Event, EmitOutcome, InMemoryEventSender, WebhookEventSender,
    emit_diff, event_id, events_for_diff, worker_joined_event,
)
from .hours import HoursRecord, ReconciliationReport, read_agency_report, read_punch_log, read_site_feed, reconcile
from .provisioning import HttpProvisioner, InMemoryProvisioner, OAuthClientCredentials, ProvisioningError, SyncOutcome, sync_access
from .identity import WorkerRegistry
from .ingest import read_roster
from .models import MatchConfidence, RosterDiff, RosterRow, Worker
from .review import confirm, reject, ReviewResolutionError
from .store import PendingReview, Store, file_hash

__all__ = [
    "Credential",
    "CredentialStatus",
    "EligibilityReport",
    "Verdict",
    "build_report",
    "evaluate",
    "compute_diff",
    "DeliveryError", "Event", "EmitOutcome", "InMemoryEventSender", "WebhookEventSender",
    "emit_diff", "event_id", "events_for_diff", "worker_joined_event",
    "HoursRecord", "ReconciliationReport", "read_agency_report", "read_punch_log", "read_site_feed", "reconcile",
    "HttpProvisioner", "InMemoryProvisioner", "OAuthClientCredentials", "ProvisioningError", "SyncOutcome", "sync_access",
    "WorkerRegistry",
    "read_roster",
    "MatchConfidence",
    "RosterDiff",
    "RosterRow",
    "Worker",
    "Store",
    "PendingReview",
    "file_hash",
    "confirm",
    "reject",
    "ReviewResolutionError",
]
