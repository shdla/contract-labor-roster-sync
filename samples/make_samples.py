"""Generate sample agency roster workbooks with realistic defects.

Every quirk here is one the parser must survive: a title row above the
headers, a blank spacer row, phone numbers stored three different ways, a
missing email, a name typo between weeks, a changed phone number, and a
trailing notes row below the data.

Run: python samples/make_samples.py
"""

from pathlib import Path

from openpyxl import Workbook

HERE = Path(__file__).parent

WEEK_1 = [
    ["Marcus", "Webb", "(832) 555-0142", "mwebb@example.com", "Material Handler"],
    ["Danielle", "Okonkwo", "832.555.0178", "dokonkwo@example.com", "Pod Production"],
    ["Ray", "Villanueva Jr", "8325550193", "", "Forklift Operator"],
    ["Tomas", "Ruiz", 8325550214, "truiz@example.com", "MH"],
    ["Priya", "Raghunathan", "1-832-555-0227", "praghunathan@example.com", "Buckhoist Operator"],
    ["Curtis", "Delaney", "n/a", "cdelaney@example.com", "material handler"],
]

# Week 2: Webb keeps his number but the agency typo'd his first name.
# Okonkwo has a new phone and the same email. Villanueva is gone.
# Two new starters, one of whom shares a surname with nobody.
WEEK_2 = [
    ["Marcuss", "Webb", "(832) 555-0142", "mwebb@example.com", "Material Handler"],
    ["Danielle", "Okonkwo", "832-555-0301", "dokonkwo@example.com", "Pod Production"],
    ["Tomas", "Ruiz", 8325550214, "truiz@example.com", "MH"],
    ["Priya", "Raghunathan", "1-832-555-0227", "praghunathan@example.com", "Buckhoist Operator"],
    ["Curtis", "Delaney", "832 555 0266", "cdelaney@example.com", "material handler"],
    ["Alicia", "Fontenot", "(832) 555-0288", "afontenot@example.com", "Pod Prod"],
    ["Jerome", "Baptiste", "832.555.0299", "", "Forklift"],
]


def build(rows: list[list], path: Path, title: str) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Roster"

    # Title row above the headers, exactly as agencies send them.
    worksheet.append([title])
    worksheet.append([])
    worksheet.append(["First Name", "Last Name", "Phone Number", "Email", "Position"])

    for row in rows:
        worksheet.append(row)

    # Trailing note below the data.
    worksheet.append([])
    worksheet.append(["Questions? Contact the branch office."])

    workbook.save(path)
    print(f"wrote {path}")


if __name__ == "__main__":
    build(WEEK_1, HERE / "roster_week1.xlsx", "Placement Roster - Week of 2024-07-08")
    build(WEEK_2, HERE / "roster_week2.xlsx", "Placement Roster - Week of 2024-07-15")
