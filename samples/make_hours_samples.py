"""Generate the agency hours file for the reconciliation demo.

run_pipeline.py writes the scanner and site files, which need the worker ids
issued at roster ingest. Together the three deliberately contain one clean
worker, one over-reported day, one day present at the work area but never
badged at the gate, and one badge id with no mapping.
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

print("wrote agency_hours.csv")
