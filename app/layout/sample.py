"""The only data that ever leaves the service for an LLM: the top rows of each
sheet, with personal data masked.

Masking keeps what layout detection needs (header texts, the *shape* of dates
and amounts, banking keywords like 'Opening Balance' or 'NEFT') and hides what
it doesn't (names, account and reference numbers).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from ..cleaners import CleanError, as_text, looks_like_date, parse_amount
from ..reader import RawRow

SAMPLE_ROWS = 40
MAX_SHEETS = 3
MAX_CELL_CHARS = 60

KEEP_WORDS = {
    "OPENING", "CLOSING", "BALANCE", "BAL", "TOTAL", "GRAND", "PAGE", "OF", "BROUGHT", "CARRIED", "FORWARD",
    "STATEMENT", "SUMMARY", "PERIOD", "FROM", "TO", "DEBIT", "CREDIT", "DEBITS", "CREDITS",
    "DR", "CR", "NEFT", "IMPS", "RTGS", "UPI", "ACH", "NACH", "ECS", "TRF", "TRANSFER", "IFT", "FT", "CHQ", "CHEQUE",
    "RETURN", "RETURNED", "REVERSAL", "REV", "RTN", "CHARGES", "CHARGE", "CHGS", "GST", "INT", "INTEREST",
    "SALARY", "REIMB", "REIMBURSEMENT", "PAYOUT", "REFUND", "INVALID", "ACCOUNT", "AC", "REF", "UTR",
    "PAID", "PENDING", "FAILED", "SUCCESS", "PROCESSING", "CANCELLED", "REVERSED",
}
_WORD = re.compile(r"[A-Za-z]{3,}")
_LONG_DIGITS = re.compile(r"\d{5,}")


def _is_value_cell(v: Any) -> bool:
    """Dates and amounts: their exact shape matters for detection, so they are kept."""
    if isinstance(v, (int, float, date, datetime)) and not isinstance(v, bool):
        return True
    if isinstance(v, str) and v.strip():
        if looks_like_date(v):
            return True
        try:
            return parse_amount(v) is not None
        except CleanError:
            return False
    return False


def _mask_digits(text: str) -> str:
    return _LONG_DIGITS.sub(lambda m: "x" * (len(m.group()) - 2) + m.group()[-2:], text)


def _mask_words(text: str) -> str:
    return _WORD.sub(lambda m: m.group() if m.group().upper() in KEEP_WORDS else m.group()[0] + "x" * (len(m.group()) - 1), text)


def mask_cell(v: Any, data_row: bool) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return f"<date {v.date().isoformat()}>"
    if isinstance(v, date):
        return f"<date {v.isoformat()}>"
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"<number {v}>"
    if _is_value_cell(v):
        return as_text(v)
    text = _mask_digits(as_text(v))
    # Header and preamble rows hold column names and labels: keep the words. Data rows hold names: mask them.
    return _mask_words(text) if data_row else text


def render_sample(sheets: dict[str, list[RawRow]], mask: bool = True, rows: int = SAMPLE_ROWS) -> str:
    out: list[str] = []
    for sheet, sheet_rows in list(sheets.items())[:MAX_SHEETS]:
        out.append(f"=== Sheet '{sheet}' ({len(sheet_rows)} rows in total; first {min(rows, len(sheet_rows))} shown) ===")
        for row in sheet_rows[:rows]:
            data_row = any(_is_value_cell(v) for v in row.values)
            cells = [(mask_cell(v, data_row) if mask else as_text(v))[:MAX_CELL_CHARS] for v in row.values]
            out.append(f"R{row.row_number}: " + " | ".join(f"[{c}]" for c in cells))
    return "\n".join(out)
