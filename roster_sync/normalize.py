"""Normalization of raw roster values into comparable forms.

Every matching decision downstream compares normalized values, never raw ones.
Normalization is deliberately conservative: when a value cannot be normalized
with confidence it becomes None rather than a guess, because a wrong
normalization silently merges two people.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Values agencies put in a cell when they do not have the real thing.
# Anything matching these is treated as absent, not as a value.
PLACEHOLDER_TOKENS = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "null",
    "tbd",
    "unknown",
    "no email",
    "noemail",
    "no phone",
    "pending",
    "x",
    "xx",
    "xxx",
}

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

_NON_DIGIT = re.compile(r"\D")
_MULTI_SPACE = re.compile(r"\s+")
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean(raw: object) -> str:
    """Trim, collapse whitespace, and drop placeholder tokens."""
    if raw is None:
        return ""
    text = str(raw).strip()
    text = _MULTI_SPACE.sub(" ", text)
    if text.lower() in PLACEHOLDER_TOKENS:
        return ""
    return text


def normalize_phone(raw: object, default_country_code: str = "1") -> str | None:
    """Return an E.164 phone string, or None if the value is unusable.

    Handles the formats that show up in agency spreadsheets: (832) 555-0142,
    832.555.0142, 8325550142, 1-832-555-0142, and values Excel stored as
    floats such as 8325550142.0.
    """
    text = _clean(raw)
    if not text:
        return None

    # Excel stores an unformatted phone column as a float.
    if text.endswith(".0"):
        text = text[:-2]

    # Drop anything after an extension marker; extensions are not identity.
    # The marker may abut its digits ("x204"), so match on the following
    # digit rather than on a trailing word boundary.
    text = re.split(r"(?i)(?:\s|^)(?:x|ext\.?|extension)\s*(?=\d)", text)[0]

    digits = _NON_DIGIT.sub("", text)
    if not digits:
        return None

    if len(digits) == 10:
        digits = default_country_code + digits
    elif len(digits) == 11 and digits.startswith(default_country_code):
        pass
    else:
        # Wrong length: a truncated cell, a partial number, or a fax with a
        # country code we cannot infer. Refuse rather than guess.
        return None

    # A number of all-identical digits is a filler value, not a phone.
    if len(set(digits[1:])) == 1:
        return None

    return "+" + digits


def normalize_email(raw: object) -> str | None:
    """Return a lowercased email, or None if absent or not email-shaped."""
    text = _clean(raw)
    if not text:
        return None
    text = text.lower()
    if not _EMAIL_SHAPE.match(text):
        return None
    return text


@dataclass(frozen=True)
class NormalizedName:
    first: str
    last: str

    @property
    def key(self) -> str:
        """Comparable form: 'last|first', accent-folded and lowercased."""
        return f"{self.last}|{self.first}"

    @property
    def display(self) -> str:
        return f"{self.first.title()} {self.last.title()}".strip()


def _fold(text: str) -> str:
    """Lowercase, strip accents, and remove punctuation used in names."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    stripped = stripped.lower()
    stripped = re.sub(r"[.'\u2019]", "", stripped)
    stripped = re.sub(r"[-_]+", " ", stripped)
    return _MULTI_SPACE.sub(" ", stripped).strip()


def normalize_name(first: object, last: object) -> NormalizedName | None:
    """Return a NormalizedName, or None if either part is missing.

    Also handles the common case of a single cell holding "Last, First" that
    was mapped to the first-name column.
    """
    first_text = _clean(first)
    last_text = _clean(last)

    if first_text and not last_text and "," in first_text:
        last_part, _, first_part = first_text.partition(",")
        last_text, first_text = last_part, first_part

    first_folded = _fold(first_text)
    last_folded = _fold(last_text)

    # Drop generational suffixes: they appear inconsistently week to week.
    last_parts = [p for p in last_folded.split() if p not in NAME_SUFFIXES]
    last_folded = " ".join(last_parts)

    # Keep only the first given name; middle names appear intermittently.
    first_parts = first_folded.split()
    first_folded = first_parts[0] if first_parts else ""

    if not first_folded or not last_folded:
        return None

    return NormalizedName(first=first_folded, last=last_folded)


def normalize_role(raw: object, role_map: dict[str, str]) -> str | None:
    """Map a free-text role cell onto a canonical role code.

    role_map is loaded from config so a new spelling is a config change, not
    a code change. Unmapped roles return None; nothing is raised. A worker
    with no mapped role is blocked by the eligibility gate.
    """
    text = _clean(raw)
    if not text:
        return None
    return role_map.get(_fold(text))
