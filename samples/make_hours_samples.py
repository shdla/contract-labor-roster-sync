"""Generate sample hours files for the reconciliation demo.

Deliberately contains one clean worker, one over-reported day, one day
present at the work area but never badged at the gate, and one badge id
with no mapping.
"""
from pathlib import Path

HERE = Path(__file__).parent

(HERE / "agency_hours.csv").write_text(
    "First Name,Last Name,Phone,Email,Date,Hours\n"
    "Tomas,Ruiz,(832) 555-0214,truiz@example.com,2024-07-15,8\n"
    "Tomas,Ruiz,(832) 555-0214,truiz@example.com,07/16/2024,8\n"
    "Alicia,Fontenot,(832) 555-0288,afontenot@example.com,2024-07-15,8\n"
    "Alicia,Fontenot,(832) 555-0288,afontenot@example.com,2024-07-16,8\n"
)

(HERE / "scanner_punches.csv").write_text(
    "worker_id,date,in,out\n"
)

(HERE / "site_badge_feed.csv").write_text(
    "badge_id,date,in,out\n"
)

print("wrote agency_hours.csv; scanner and site files are written by the demo,\n"
      "which needs the worker ids issued at roster ingest")
