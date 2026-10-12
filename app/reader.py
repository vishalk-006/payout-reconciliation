"""Raw spreadsheet reading. No interpretation here: every cell comes back as-is,
with its real Excel row number, so every later decision can point back to the file."""
from __future__ import annotations

import csv
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openpyxl

SUPPORTED_SUFFIXES = {".xlsx", ".xlsm", ".csv"}


class ReadError(ValueError):
    pass


@dataclass
class RawRow:
    row_number: int  # 1-based, as finance sees it in Excel
    values: list[Any]

    def get(self, col: int | None) -> Any:
        if col is None or col >= len(self.values):
            return None
        return self.values[col]


def read_sheets(path: Path) -> dict[str, list[RawRow]]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise ReadError(f"Unsupported file type '{suffix}'. Use .xlsx or .csv (old .xls: re-save as .xlsx).")
    if suffix == ".csv":
        return {"csv": _read_csv(path)}
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        raise ReadError(f"'{path.name}' is not a readable Excel file ({exc.__class__.__name__}).") from exc
    try:
        sheets: dict[str, list[RawRow]] = {}
        for ws in wb.worksheets:
            if getattr(ws, "sheet_state", "visible") != "visible":
                continue
            rows = []
            for n, values in enumerate(ws.iter_rows(min_row=1, values_only=True), start=1):
                rows.append(RawRow(n, _trim(list(values))))
            sheets[ws.title] = rows
        return sheets
    finally:
        wb.close()


def _read_csv(path: Path) -> list[RawRow]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ReadError(f"'{path.name}' has an unknown text encoding.")
    return [RawRow(n, _trim(r)) for n, r in enumerate(csv.reader(text.splitlines()), start=1)]


def _trim(values: list[Any]) -> list[Any]:
    while values and (values[-1] is None or (isinstance(values[-1], str) and not values[-1].strip())):
        values.pop()
    return values
