"""Strict field cleaners.

Each cleaner either returns a value or raises CleanError with a code and a
message a finance user can read. They never guess: a value that does not match
a known shape is rejected, not "fixed".
"""
from __future__ import annotations

import math
import re
from itertools import pairwise
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from collections.abc import Iterable

CENT = Decimal("0.01")
MIN_DATE = date(2000, 1, 1)
MAX_DATE = date(2100, 1, 1)


class CleanError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_blank(raw: Any) -> bool:
    return raw is None or (isinstance(raw, str) and not raw.strip())


def as_text(raw: Any) -> str:
    """Cell value as display text. Integral floats lose the '.0' (IDs exported as numbers)."""
    if raw is None:
        return ""
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    if isinstance(raw, datetime):
        return raw.date().isoformat() if raw.time() == datetime.min.time() else raw.isoformat(sep=" ")
    if isinstance(raw, date):
        return raw.isoformat()
    return re.sub(r"\s+", " ", str(raw)).strip()


def normalize_text(raw: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", as_text(raw).upper()).strip()


def normalize_header(raw: Any) -> str:
    """'Chq./Ref.No.' -> 'chqrefno'"""
    return re.sub(r"[^a-z0-9]+", "", as_text(raw).lower())


def compact(raw: Any) -> str:
    """'PO-101' -> 'PO101'"""
    return re.sub(r"[^A-Z0-9]+", "", as_text(raw).upper())


EMPTY_AMOUNT = {"", "-", "--", "NIL", "NA", "N/A", "0.00-"}

_AMOUNT_RE = re.compile(
    r"""^
    (?P<open>\()?
    (?P<sign>[-+])?
    (?:INR|RS\.?|₹)?
    (?P<sign2>[-+])?
    (?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)
    (?P<trail>-)?
    (?P<close>\))?
    (?P<marker>DR|CR)?\.?
    $""",
    re.X,
)


@dataclass(frozen=True)
class Amount:
    value: Decimal
    marker: str | None


def _to_cents(d: Decimal, raw: Any) -> Decimal:
    q = d.quantize(CENT, rounding=ROUND_HALF_UP)
    # Tolerate binary-float noise (1199.9999999) but not real sub-paisa values (12.345).
    if abs(q - d) > Decimal("0.0001"):
        raise CleanError("TOO_MANY_DECIMALS", f"Amount '{raw}' has more than 2 decimal places")
    return q


def parse_amount(raw: Any) -> Amount | None:
    """Returns None for an explicitly empty cell; raises CleanError for anything unrecognisable."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise CleanError("UNPARSEABLE_AMOUNT", f"'{raw}' is not an amount")
    if isinstance(raw, (int, float, Decimal)):
        if isinstance(raw, float) and not math.isfinite(raw):
            raise CleanError("UNPARSEABLE_AMOUNT", f"'{raw}' is not an amount")
        return Amount(_to_cents(Decimal(str(raw)), raw), None)
    if isinstance(raw, (date, datetime)):
        raise CleanError("UNPARSEABLE_AMOUNT", f"Found a date ('{as_text(raw)}') where an amount was expected")

    s = re.sub(r"\s+", "", str(raw).replace(" ", " ")).upper()
    if s in EMPTY_AMOUNT:
        return None
    m = _AMOUNT_RE.match(s)
    if not m:
        hint = " (it looks like the letter O was typed instead of a zero)" if re.search(r"\dO|O\d", s) else ""
        raise CleanError("UNPARSEABLE_AMOUNT", f"'{raw}' is not a recognisable amount{hint}")
    if bool(m["open"]) != bool(m["close"]):
        raise CleanError("UNPARSEABLE_AMOUNT", f"'{raw}' has unbalanced parentheses")
    signs = [x for x in (m["sign"], m["sign2"], m["trail"]) if x]
    if len(signs) > 1 or (signs and m["open"]):
        raise CleanError("UNPARSEABLE_AMOUNT", f"'{raw}' has conflicting signs")
    value = _to_cents(Decimal(m["num"].replace(",", "")), raw)
    if m["open"] or (signs and signs[0] == "-"):
        value = -value
    return Amount(value, m["marker"])


# Formats are applied after separators are normalised to '-' (see _norm_date_text).
DEFAULT_DATE_FORMATS = [
    "%d-%m-%Y", "%d-%m-%y", "%d-%b-%Y", "%d-%b-%y", "%d-%B-%Y",
    "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y", "%b-%d-%Y", "%d%m%Y", "%Y%m%d",
]
UNAMBIGUOUS_FORMATS = ["%Y-%m-%d", "%d-%b-%Y", "%d-%b-%y", "%d-%B-%Y", "%b-%d-%Y"]

_TIME_SUFFIX = re.compile(r"(?:[ T]+\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?\s*(?:AM|PM)?)$", re.I)
_SERIAL_TEXT = re.compile(r"^\d{5}(?:\.\d+)?$")


class DateFormatError(ValueError):
    pass


def _norm_date_text(s: str) -> str:
    s = _TIME_SUFFIX.sub("", s.strip())
    return re.sub(r"[\s\-./,]+", "-", s).strip("-")


def _check_range(d: date, raw: Any) -> date:
    if not (MIN_DATE <= d < MAX_DATE):
        raise CleanError("DATE_OUT_OF_RANGE", f"Date '{as_text(raw)}' is outside 2000-2099")
    return d


def excel_serial_to_date(n: float) -> date:
    # Excel's day 0 is 1899-12-30 once the fake 1900 leap day is accounted for.
    return date(1899, 12, 30) + timedelta(days=int(n))


def _try_strptime(text: str, fmt: str) -> date | None:
    try:
        return datetime.strptime(text, fmt).date()
    except ValueError:
        return None


def is_textual_date(raw: Any) -> bool:
    return isinstance(raw, str) and bool(raw.strip()) and not _SERIAL_TEXT.match(raw.strip())


def looks_like_date(raw: Any) -> bool:
    """Cheap shape test used to tell a dated row from a footer/note in the date column."""
    if isinstance(raw, (date, datetime)) or (isinstance(raw, (int, float)) and not isinstance(raw, bool)):
        return True
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 30:
        return False
    text = _norm_date_text(raw)
    return bool(_SERIAL_TEXT.match(raw.strip())) or any(_try_strptime(text, f) for f in DEFAULT_DATE_FORMATS) \
        or bool(re.match(r"^\d{1,4}-[A-Za-z0-9]{1,9}-\d{1,4}$", text))


def parse_date(raw: Any, column_format: str | None) -> tuple[date | None, str | None]:
    """Returns (date, warning)."""
    if is_blank(raw):
        return None, None
    if isinstance(raw, datetime):
        return _check_range(raw.date(), raw), None
    if isinstance(raw, date):
        return _check_range(raw, raw), None
    if isinstance(raw, bool):
        raise CleanError("UNPARSEABLE_DATE", f"'{raw}' is not a date")
    if isinstance(raw, (int, float)) or _SERIAL_TEXT.match(str(raw).strip()):
        return _check_range(excel_serial_to_date(float(raw)), raw), None

    text = _norm_date_text(str(raw))
    if column_format and (d := _try_strptime(text, column_format)):
        return _check_range(d, raw), None
    for alt in UNAMBIGUOUS_FORMATS:
        if alt != column_format and (d := _try_strptime(text, alt)):
            return _check_range(d, raw), f"Date '{raw}' used a different format than the rest of the column"
    raise CleanError("UNPARSEABLE_DATE", f"'{raw}' is not a recognisable date")


def _monotonic(dates: list[date]) -> bool:
    pairs = list(pairwise(dates))
    return all(a <= b for a, b in pairs) or all(a >= b for a, b in pairs)


def infer_date_format(values: Iterable[Any], preferred: Iterable[str] = ()) -> str | None:
    """Pick ONE format for a whole column, using every value.

    Order of decision:
      1. formats that parse the most values win;
      2. among those, a format consistent with the layout's date order wins;
      3. formats that read every value identically are the same answer;
      4. if dd/mm vs mm/dd is still open, the reading that keeps rows in date order wins;
      5. otherwise refuse to guess.
    """
    texts = [v for v in values if is_textual_date(v)]
    if not texts:
        return None
    preferred = list(preferred)
    candidates = list(dict.fromkeys([*preferred, *DEFAULT_DATE_FORMATS]))
    readings: dict[str, list[date | None]] = {}
    for fmt in candidates:
        readings[fmt] = [_try_strptime(_norm_date_text(t), fmt) for t in texts]
    score = {f: sum(d is not None for d in r) for f, r in readings.items()}
    best = max(score.values())
    if best == 0:
        raise DateFormatError(f"No known date format fits values such as {texts[:3]}")
    top = [f for f in candidates if score[f] == best]

    for f in preferred:
        if f in top:
            return f

    distinct: list[str] = []
    for f in top:
        if not any(readings[f] == readings[g] for g in distinct):
            distinct.append(f)
    if len(distinct) == 1:
        return distinct[0]

    ordered = [f for f in distinct if _monotonic([d for d in readings[f] if d])]
    if len(ordered) == 1:
        return ordered[0]
    raise DateFormatError(
        f"Dates such as {texts[:3]} can be read as more than one format ({', '.join(distinct)}). "
        f"The layout does not say whether dates are day-first or month-first."
    )
