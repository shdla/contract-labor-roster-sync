"""Contract labor roster sync: ingest, identity resolution, diffing, persistence,
review, eligibility, provisioning, hours reconciliation, event emission.

The root re-exports only the names the repository imports from it, which is
what samples/run_pipeline.py uses. Everything else is imported from its
submodule, as the tests do.
"""

from .credentials import Credential, build_report
from .diff import compute_diff
from .events import InMemoryEventSender, emit_diff
from .hours import read_agency_report, read_punch_log, read_site_feed, reconcile
from .ingest import read_roster
from .provisioning import InMemoryProvisioner, sync_access
from .store import Store, file_hash

__all__ = [
    "Credential",
    "InMemoryEventSender",
    "InMemoryProvisioner",
    "Store",
    "build_report",
    "compute_diff",
    "emit_diff",
    "file_hash",
    "read_agency_report",
    "read_punch_log",
    "read_roster",
    "read_site_feed",
    "reconcile",
    "sync_access",
]
