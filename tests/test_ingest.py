from pathlib import Path

import pytest
import yaml
from openpyxl import Workbook

from roster_sync.ingest import DEFAULT_HEADER_ALIASES, find_header_row, read_roster

# Resolved from this file, not the cwd, so the suite runs from any directory.
ROOT = Path(__file__).resolve().parent.parent
with open(ROOT / "config" / "roles.yaml") as handle:
    ROLE_MAP = yaml.safe_load(handle)["aliases"]

HEADERS = ("First Name", "Last Name", "Phone", "Email", "Role")


# -- header detection -----------------------------------------------------


def test_title_row_is_skipped_and_the_header_index_returned():
    grid = [
        ("Placement Roster - Week of 2024-07-08",),
        ("Assignment", "Building 4"),  # scores one; the real header scores five
        (),
        HEADERS,
    ]
    index, column_map = find_header_row(grid, DEFAULT_HEADER_ALIASES)
    assert index == 3, "zero-based; read_roster reports it one-based"
    assert column_map == {"first_name": 0, "last_name": 1, "phone": 2, "email": 3, "role": 4}


def test_alias_spellings_map_to_canonical_fields():
    grid = [("Surname", "Given Name", "Cell", "E-mail", "Position")]
    _, column_map = find_header_row(grid, DEFAULT_HEADER_ALIASES)
    assert column_map == {"last_name": 0, "first_name": 1, "phone": 2, "email": 3, "role": 4}


def test_duplicate_header_keeps_the_first_column():
    grid = [("First Name", "Last Name", "Phone", "Mobile", "Email")]
    _, column_map = find_header_row(grid, DEFAULT_HEADER_ALIASES)
    assert column_map["phone"] == 2
    assert column_map["email"] == 4


def test_grid_without_a_header_raises():
    grid = [("Placement Roster",), ("Marcus", "Webb", "(832) 555-0142")]
    with pytest.raises(ValueError):
        find_header_row(grid, DEFAULT_HEADER_ALIASES)


def test_header_below_scan_depth_raises():
    filler = [("note",)] * 14
    assert find_header_row(filler + [HEADERS], DEFAULT_HEADER_ALIASES)[0] == 14, "row fifteen is still scanned"
    with pytest.raises(ValueError):
        find_header_row(filler + [("note",), HEADERS], DEFAULT_HEADER_ALIASES)


# -- workbooks ------------------------------------------------------------


def write_workbook(path, grid):
    """Same construction as samples/make_samples.py; an empty list is a blank row."""
    workbook = Workbook()
    for cells in grid:
        workbook.active.append(cells)
    workbook.save(path)
    return path


MESSY = [
    ["Placement Roster - Week of 2024-07-08"],
    [],
    HEADERS,
    ["Marcus", "Webb", "(832) 555-0142", "mwebb@example.com", "Material Handler"],  # Excel row 4
    [],
    ["Tomas", "Ruiz", 8325550214, "truiz@example.com", "MH"],  # Excel row 6
    [],
    ["Questions? Contact the branch office."],  # Excel row 8
]


@pytest.fixture
def messy(tmp_path):
    return read_roster(write_workbook(tmp_path / "roster.xlsx", MESSY), ROLE_MAP)


def test_source_row_is_the_excel_row_and_blank_rows_are_counted_not_emitted(messy):
    rows, report = messy
    assert report.header_row == 3
    assert [r.source_row for r in rows] == [4, 6, 8]
    assert (report.rows_read, report.rows_blank) == (3, 2)


def test_trailing_note_row_is_emitted_as_unusable(messy):
    rows, _ = messy
    assert [r.is_usable for r in rows] == [True, True, False]
    assert rows[-1].name is None


def test_numeric_phone_cell_normalizes(messy):
    rows, _ = messy
    assert rows[1].phone == "+18325550214"
    assert rows[1].role == "material_handler"


def test_tracked_sample_parses_as_the_readme_describes():
    rows, report = read_roster(ROOT / "samples" / "roster_week1.xlsx", ROLE_MAP)
    assert (report.header_row, report.rows_read, report.rows_blank) == (3, 7, 1)
    assert [r.source_row for r in rows if not r.is_usable] == [11], "the trailing note"


def test_custom_header_aliases_replace_the_defaults(tmp_path):
    path = write_workbook(tmp_path / "roster.xlsx", [
        ["Associate First", "Associate Last", "Contact #"],
        ["Marcus", "Webb", "(832) 555-0142"],
    ])
    with pytest.raises(ValueError):
        read_roster(path, ROLE_MAP)

    aliases = {"first_name": ["associate first"], "last_name": ["associate last"], "phone": ["contact #"]}
    rows, report = read_roster(path, ROLE_MAP, header_aliases=aliases)
    assert report.column_map == {"first_name": 0, "last_name": 1, "phone": 2}
    assert rows[0].is_usable and rows[0].phone == "+18325550142"
