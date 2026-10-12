"""Layout objects and their deterministic validation.

DetectedLayout  what an LLM returns (strict JSON schema).
Layout          what we store and reuse; identified by a fingerprint of the header row.
HeaderMatch     a Layout bound to a concrete file: sheet, header row, column indexes.

An LLM answer is never trusted as-is: header_from_detection() checks that every
column it names really exists in the row it pointed at, and the parsers then
prove the layout on the whole file (see resolver.py).
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal, TypedDict

from pydantic import BaseModel, Field

from ..cleaners import as_text, normalize_header
from ..reader import RawRow
from .spec import KindSpec

DateOrder = Literal["DMY", "MDY", "YMD", "UNKNOWN"]
NegativeMeans = Literal["DEBIT", "CREDIT", "NOT_APPLICABLE"]

DATE_FORMATS_BY_ORDER: dict[str, list[str]] = {
    "DMY": ["%d-%m-%Y", "%d-%m-%y", "%d-%b-%Y", "%d-%b-%y", "%d-%B-%Y", "%d%m%Y"],
    "MDY": ["%m-%d-%Y", "%m-%d-%y", "%b-%d-%Y"],
    "YMD": ["%Y-%m-%d", "%Y%m%d"],
    "UNKNOWN": [],
}


SEEDED = "seeded"  # Layout.source of layouts shipped with the service and reviewed by a person
REPEATED_HEADER_SHARE = 0.6  # a later row repeating this share of the header texts is a page-break header


class ColumnRef(TypedDict):
    header: str
    index: int


class LayoutError(ValueError):
    """The layout could not be determined or did not hold up against the file."""


class WrongFileKind(LayoutError):
    """The file is not the kind of file expected (e.g. a payout register sent as the bank statement)."""


class ColumnAssignment(BaseModel):
    field: str = Field(description="One of the canonical field names listed in the instructions.")
    header: str = Field(description="The header cell text copied exactly as it appears in the file.")


class DetectedLayout(BaseModel):
    is_expected_kind: bool = Field(description="False if this file is clearly not the expected kind of file.")
    sheet: str = Field(description="Name of the sheet that holds the table.")
    header_row: int = Field(description="Row number (the R number shown) of the table's column header row.")
    columns: list[ColumnAssignment] = Field(description="Canonical field -> header text, only for columns present.")
    date_order: DateOrder = Field(description="Day/month/year order used by the date values.")
    negative_amount_means: NegativeMeans = Field(
        description="For a single signed amount column without a Dr/Cr column: what a negative number means.")
    notes: str = Field(description="One or two sentences explaining the decisions, for an auditor.")


def header_cells(row: RawRow) -> list[str]:
    return [as_text(v) for v in row.values if isinstance(v, str) and v.strip()]


def fingerprint(cells: list[str]) -> str:
    key = "|".join(normalize_header(c) for c in cells)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass
class Layout:
    kind: str
    header: list[str]  # the header row's non-empty cells, in order
    columns: dict[str, ColumnRef]
    date_order: str = "UNKNOWN"
    negative_amount_means: str = "NOT_APPLICABLE"
    source: str = SEEDED  # or "<provider>:<model>" for a layout an LLM identified
    verified: bool = False  # reviewed by a person, or proven on a whole file
    notes: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.header)

    @property
    def id(self) -> str:
        return f"{self.kind}-{self.fingerprint}"

    @property
    def date_formats(self) -> list[str]:
        return DATE_FORMATS_BY_ORDER.get(self.date_order, [])

    def to_dict(self) -> dict:
        return {"id": self.id, "fingerprint": self.fingerprint, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict) -> Layout:
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class HeaderMatch:
    layout: Layout
    sheet: str
    row_index: int  # index into the sheet's row list
    row_number: int
    columns: dict[str, int]
    headers: dict[str, str]

    @property
    def date_formats(self) -> list[str]:
        return self.layout.date_formats

    @property
    def negative_amount_means(self) -> str:
        return self.layout.negative_amount_means


def bind(layout: Layout, sheet: str, rows: list[RawRow], row_index: int) -> HeaderMatch:
    """Attach a stored layout to the header row found in a file (fingerprints already match)."""
    row = rows[row_index]
    columns, headers = {}, {}
    for f, col in layout.columns.items():
        idx = col["index"]
        if idx >= len(row.values) or normalize_header(row.values[idx]) != normalize_header(col["header"]):
            raise LayoutError(f"Stored layout {layout.id} expects '{col['header']}' in column {idx + 1}")
        columns[f], headers[f] = idx, as_text(row.values[idx])
    return HeaderMatch(layout, sheet, row_index, row.row_number, columns, headers)


def header_from_detection(sheets: dict[str, list[RawRow]], det: DetectedLayout, spec: KindSpec,
                          source: str) -> HeaderMatch:
    """Check an LLM answer against the file. Raises LayoutError with a message the LLM can act on."""
    if not det.is_expected_kind:
        raise WrongFileKind(f"This file does not look like a {spec.title}. {det.notes}".strip())
    if det.sheet not in sheets:
        raise LayoutError(f"Sheet '{det.sheet}' does not exist; sheets are {list(sheets)}.")
    rows = sheets[det.sheet]
    row_index = next((i for i, r in enumerate(rows) if r.row_number == det.header_row), None)
    if row_index is None:
        raise LayoutError(f"Row {det.header_row} does not exist in sheet '{det.sheet}'.")
    row = rows[row_index]
    by_text: dict[str, list[int]] = {}
    for i, v in enumerate(row.values):
        if isinstance(v, str) and v.strip():
            by_text.setdefault(normalize_header(v), []).append(i)

    problems: list[str] = []
    columns: dict[str, ColumnRef] = {}
    used: dict[int, str] = {}
    for a in det.columns:
        if a.field not in spec.field_names:
            problems.append(f"'{a.field}' is not a valid field name.")
            continue
        if a.field in columns:
            problems.append(f"Field '{a.field}' was assigned twice.")
            continue
        hits = by_text.get(normalize_header(a.header), [])
        if not hits:
            problems.append(f"Header '{a.header}' (for {a.field}) is not in row {det.header_row}.")
            continue
        idx = next((h for h in hits if h not in used), None)
        if idx is None:
            problems.append(f"Column '{a.header}' is already used for '{used[hits[0]]}'.")
            continue
        used[idx] = a.field
        columns[a.field] = {"header": as_text(row.values[idx]), "index": idx}
    if missing := spec.missing(set(columns)):
        problems.append(f"Required fields not mapped: {', '.join(missing)}.")
    if problems:
        raise LayoutError(" ".join(problems))

    layout = Layout(kind=spec.kind, header=header_cells(row), columns=columns, date_order=det.date_order,
                    negative_amount_means=det.negative_amount_means, source=source, notes=det.notes)
    return bind(layout, det.sheet, rows, row_index)


def is_header_like(row: RawRow, header: HeaderMatch) -> bool:
    """A repeated header (e.g. at a page break) shows most of the header texts in the same columns."""
    same = sum(1 for f, c in header.columns.items()
               if normalize_header(row.get(c)) == normalize_header(header.headers[f]))
    return same >= max(2, math.ceil(len(header.columns) * REPEATED_HEADER_SHARE))
