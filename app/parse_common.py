"""Helpers shared by the payout-register and bank-statement parsers."""
from __future__ import annotations

from .cleaners import as_text, is_blank
from .layout.models import HeaderMatch
from .models import Issue, Severity
from .reader import RawRow


def raw_dict(row: RawRow, header: HeaderMatch) -> dict[str, str]:
    """Header text -> cell text, in the file's column order (independent of how the layout listed them)."""
    ordered = sorted(header.columns.items(), key=lambda item: item[1])
    return {header.headers[f]: as_text(row.get(c)) for f, c in ordered}


def unread_sheet_issues(sheets: dict[str, list[RawRow]], header: HeaderMatch, file_name: str) -> list[Issue]:
    """Only the header's sheet is parsed; other sheets with content must not disappear silently."""
    issues = []
    for name, rows in sheets.items():
        if name == header.sheet:
            continue
        filled = sum(1 for r in rows if any(not is_blank(v) for v in r.values))
        if filled:
            issues.append(Issue(Severity.ERROR, file_name, None, "UNREAD_SHEET",
                                f"Sheet '{name}' has {filled} non-empty row(s) that were NOT reconciled; only sheet "
                                f"'{header.sheet}' was read. If it continues the data, combine the sheets and re-upload."))
    return issues
