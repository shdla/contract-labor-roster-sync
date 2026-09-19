"""Reading an agency roster workbook into normalized rows.

Agency spreadsheets are written for humans, not for parsers. They carry a
title row above the headers, blank spacer rows, trailing notes below the
data, and columns whose headers differ week to week. This module locates the
header row rather than assuming row 1, maps columns through header aliases
rather than position, and reports what it could not understand instead of
failing on the first bad cell.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from .models import RosterRow
from .normalize import (
    normalize_email,
    normalize_name,
    normalize_phone,
    normalize_role,
)

# Canonical field -> header spellings seen in the wild. Extending this is a
# one-line change here, or pass header_aliases to read_roster; nothing in the
# parsing logic knows about column order.
DEFAULT_HEADER_ALIASES: dict[str, list[str]] = {
    "first_name": ["first name", "first", "firstname", "given name", "fname"],
    "last_name": ["last name", "last", "lastname", "surname", "lname"],
    "phone": ["phone", "phone number", "mobile", "cell", "cell phone", "contact number"],
    "email": ["email", "email address", "e-mail", "mail"],
    "role": ["role", "position", "job title", "title", "assignment", "job"],
}

REQUIRED_FIELDS = ("first_name", "last_name")


@dataclass
class IngestReport:
    """What the parser found, for logging and for the exception report."""

    path: str
    sheet: str
    header_row: int
    column_map: dict[str, int]
    missing_fields: list[str]
    rows_read: int
    rows_blank: int


def _cell_text(value: object) -> str:
    return "" if value is None else str(value).strip().lower()


def find_header_row(
    grid: list[tuple],
    aliases: dict[str, list[str]],
    scan_depth: int = 15,
) -> tuple[int, dict[str, int]]:
    """Locate the header row and map canonical fields to column indices.

    Scores each of the first scan_depth rows by how many recognized headers
    it contains, and takes the best. A title row scores zero; the real header
    row scores highest.
    """
    lookup = {s: f for f, names in aliases.items() for s in names}
    best_row, best_map, best_score = -1, {}, 0

    for index, row in enumerate(grid[:scan_depth]):
        column_map: dict[str, int] = {}
        for column, value in enumerate(row):
            field = lookup.get(_cell_text(value))
            if field and field not in column_map:
                column_map[field] = column
        if len(column_map) > best_score:
            best_row, best_map, best_score = index, column_map, len(column_map)

    if best_score == 0:
        raise ValueError(
            "no recognizable header row in the first "
            f"{scan_depth} rows; check the file or extend the header aliases"
        )
    return best_row, best_map


def read_roster(
    path: str | Path,
    role_map: dict[str, str],
    header_aliases: dict[str, list[str]] | None = None,
    sheet: str | None = None,
) -> tuple[list[RosterRow], IngestReport]:
    """Parse a roster workbook into normalized rows plus an ingest report."""
    aliases = header_aliases or DEFAULT_HEADER_ALIASES
    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    worksheet = workbook[sheet] if sheet else workbook[workbook.sheetnames[0]]

    grid = list(worksheet.iter_rows(values_only=True))
    header_row, column_map = find_header_row(grid, aliases)
    missing = [f for f in REQUIRED_FIELDS if f not in column_map]

    rows: list[RosterRow] = []
    blank = 0

    for offset, raw_row in enumerate(grid[header_row + 1:], start=header_row + 2):
        if all(cell is None or str(cell).strip() == "" for cell in raw_row):
            blank += 1
            continue

        def value(field: str) -> object:
            column = column_map.get(field)
            if column is None or column >= len(raw_row):
                return None
            return raw_row[column]

        rows.append(
            RosterRow(
                source_row=offset,
                name=normalize_name(value("first_name"), value("last_name")),
                phone=normalize_phone(value("phone")),
                email=normalize_email(value("email")),
                role=normalize_role(value("role"), role_map),
            )
        )

    workbook.close()

    report = IngestReport(
        path=str(path),
        sheet=worksheet.title,
        header_row=header_row + 1,
        column_map=column_map,
        missing_fields=missing,
        rows_read=len(rows),
        rows_blank=blank,
    )
    return rows, report
