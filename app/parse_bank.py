"""File B: Bank Statement. Treated as untrusted input.

Every row below the header is first sorted (row_kind): transaction candidate,
narration continuation, or noise with a reason. Candidates are then parsed
strictly, and the bank's own running balance proves the parse is right.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path

from .cleaners import (CleanError, as_text, infer_date_format, is_blank, looks_like_date, normalize_text,
                       parse_amount, parse_date)
from .models import (AmountStyle, BalanceCheck, BalanceStatus, BankParseResult, BankTxn, Direction, Issue, NoiseRow,
                     RejectedRow, RowAccounting, Severity)
from .parse_common import raw_dict, unread_sheet_issues
from .layout.models import HeaderMatch, is_header_like
from .layout.providers import LayoutDetector
from .layout.resolver import Check, resolve_and_parse
from .layout.spec import BANK_STATEMENT
from .layout.store import LayoutStore
from .reader import RawRow, read_sheets

OPENING_RE = re.compile(r"^(OPENING BAL(ANCE)?|BAL(ANCE)? B F|B F|BROUGHT FORWARD|BALANCE BROUGHT FORWARD)\b")
CLOSING_RE = re.compile(r"^(CLOSING BAL(ANCE)?|BAL(ANCE)? C F|C F|CARRIED FORWARD|BALANCE CARRIED FORWARD)\b")
TOTAL_RE = re.compile(r"^(TOTAL|GRAND TOTAL|PAGE TOTAL|TRANSACTION TOTAL)\b")
SUMMARY_RE = re.compile(r"^(STATEMENT SUMMARY|ACCOUNT SUMMARY|SUMMARY OF ACCOUNT)\b")
DEBIT_WORDS = {"DR", "D", "DEBIT", "WITHDRAWAL"}
CREDIT_WORDS = {"CR", "C", "CREDIT", "DEPOSIT"}
AMOUNT_FIELDS = ("debit", "credit", "amount")
BALANCE_LINE_FIELDS = ("balance", "credit", "debit", "amount")
VALUE_DATE_TOLERANCE_DAYS = 7
MAX_REJECTED_SHARE = 0.05  # more unreadable rows than this suggests the layout is wrong
ZERO = Decimal("0")


def parse_bank_statement(path: Path, file_name: str | None = None, *, store: LayoutStore | None = None,
                         detector: LayoutDetector | None = None, provider: str | None = None) -> BankParseResult:
    file_name = file_name or path.name
    return resolve_and_parse(read_sheets(path), BANK_STATEMENT, file_name,
                             lambda sheets, header: parse_bank_rows(sheets, header, file_name),
                             check_bank_parse, store=store, detector=detector, provider=provider)


def check_bank_parse(res: BankParseResult) -> Check:
    """Does the layout hold up on the whole file? The running balance is the referee."""
    problems, fatal = [], False
    total = len(res.rows) + len(res.rejected)
    if not res.rows:
        return Check(["No transaction rows could be read with this layout."], fatal=True)
    share = len(res.rejected) / total
    if share > MAX_REJECTED_SHARE:
        sample = "; ".join(f"row {r.source_row}: {r.reason}" for r in res.rejected[:3])
        problems.append(f"{len(res.rejected)} of {total} transaction rows could not be read ({sample}).")
        fatal |= share > 0.2
    status = res.balance_check.status
    if status is BalanceStatus.BROKEN:
        first = next((i.message for i in res.issues if i.code in ("BALANCE_BREAK", "CLOSING_BALANCE_MISMATCH")), "")
        problems.append(f"The running balance does not follow the parsed debits/credits at {res.balance_check.breaks} "
                        f"point(s); first: {first} Check that debit and credit (or the amount sign) are mapped the "
                        f"right way round and that the balance column is correct.")
        fatal |= res.balance_check.breaks > max(2, len(res.rows) // 5)
    # Without a balance column nothing proves debit and credit are the right way round: usable, never "verified".
    return Check(problems, fatal, proven=status is BalanceStatus.VERIFIED)


def parse_bank_rows(sheets: dict[str, list[RawRow]], header: HeaderMatch, file_name: str) -> BankParseResult:
    rows = sheets[header.sheet]
    sorted_rows = _sort_rows(rows[header.row_index + 1:], header, file_name)
    style = _amount_style(header.columns)
    issues: list[Issue] = []
    formats = _infer_date_formats(sorted_rows.candidates, header, issues, file_name)
    txns, rejected = _parse_candidates(sorted_rows, header, style, formats, file_name)
    txns = _drop_repeated_export_lines(txns, sorted_rows, issues, file_name, header)

    accounting = RowAccounting(total_rows=len(rows), preamble=header.row_index, header=1,
                               noise=len(sorted_rows.noise), parsed=len(txns), rejected=len(rejected))
    accounting.ensure_balanced(file_name)

    issues += unread_sheet_issues(sheets, header, file_name)
    issues += [Issue(Severity.WARNING, file_name, t.source_row, "ROW_WARNING", w) for t in txns for w in t.warnings]
    balance_check = _check_balances(txns, rejected, sorted_rows.opening, sorted_rows.closing, issues, file_name)
    if sorted_rows.footer_totals:
        _check_footer_totals(txns, sorted_rows.footer_totals, issues, file_name)

    return BankParseResult(file_name, header.sheet, header.row_number, txns, rejected, sorted_rows.noise, issues,
                           accounting, formats.txn_date, style, balance_check)


class RowKind(Enum):
    BLANK = "Blank row"
    REPEATED_HEADER = "Repeated header row (page break)"
    OPENING_BALANCE = "Opening balance line"
    CLOSING_BALANCE = "Closing balance line"
    TOTALS = "Totals line (used as a control check)"
    SUMMARY_START = "Statement summary starts here (footer)"
    FOOTER = "Part of the statement summary footer"
    NOTE = "Text-only row with no date and no amount (note/footer)"
    CONTINUATION = "Narration continued from the row above (merged)"
    TRANSACTION = "Transaction"


@dataclass
class SortedRows:
    candidates: list[RawRow] = field(default_factory=list)
    noise: list[NoiseRow] = field(default_factory=list)
    continued_narration: dict[int, list[str]] = field(default_factory=dict)
    opening: Decimal | None = None
    closing: Decimal | None = None
    footer_totals: tuple[Decimal | None, Decimal | None] | None = None


def row_kind(row: RawRow, header: HeaderMatch, follows_transaction: bool, in_footer: bool = False) -> RowKind:
    cols = header.columns
    labels = [normalize_text(row.get(cols.get(f))) for f in ("txn_date", "narration")]
    # "31/02/2026" is date-shaped (and rejected later); "Page 1 of 3" is not.
    has_date = looks_like_date(row.get(cols["txn_date"]))
    has_amount = any(not is_blank(row.get(cols.get(f))) for f in AMOUNT_FIELDS)

    if all(is_blank(v) for v in row.values):
        return RowKind.BLANK
    if in_footer:
        return RowKind.FOOTER
    if any(SUMMARY_RE.match(x) for x in labels):
        return RowKind.SUMMARY_START
    if is_header_like(row, header):
        return RowKind.REPEATED_HEADER
    if any(OPENING_RE.match(x) for x in labels):
        return RowKind.OPENING_BALANCE
    if any(CLOSING_RE.match(x) for x in labels):
        return RowKind.CLOSING_BALANCE
    # A dated row starting with "TOTAL" is a payment to a vendor with that name, not a totals line.
    if not has_date and any(TOTAL_RE.match(x) for x in labels):
        return RowKind.TOTALS
    if has_date or has_amount:
        return RowKind.TRANSACTION
    if follows_transaction and as_text(row.get(cols["narration"])):
        return RowKind.CONTINUATION
    return RowKind.NOTE


def _sort_rows(data_rows: list[RawRow], header: HeaderMatch, file_name: str) -> SortedRows:
    cols = header.columns
    out = SortedRows()

    def skip(row: RawRow, reason: str) -> None:
        out.noise.append(NoiseRow(file_name, row.row_number, reason, raw_dict(row, header)))

    follows_transaction = in_footer = False
    footer: list[RawRow] = []
    for row in data_rows:
        kind = row_kind(row, header, follows_transaction, in_footer)
        follows_transaction = kind in (RowKind.TRANSACTION, RowKind.CONTINUATION)
        in_footer = in_footer or kind is RowKind.SUMMARY_START
        if kind is RowKind.FOOTER:
            footer.append(row)
        match kind:
            case RowKind.TRANSACTION:
                out.candidates.append(row)
            case RowKind.CONTINUATION:
                previous = out.candidates[-1].row_number
                out.continued_narration.setdefault(previous, []).append(as_text(row.get(cols["narration"])))
                skip(row, f"Narration continued from row {previous} (merged)")
            case RowKind.OPENING_BALANCE:
                value = _first_amount(row, cols, BALANCE_LINE_FIELDS)
                out.opening = value if out.opening is None else out.opening
                skip(row, f"{kind.value} ({_fmt(value)})")
            case RowKind.CLOSING_BALANCE:
                out.closing = _first_amount(row, cols, BALANCE_LINE_FIELDS)
                skip(row, f"{kind.value} ({_fmt(out.closing)})")
            case RowKind.TOTALS:
                out.footer_totals = (_first_amount(row, cols, ("debit",)), _first_amount(row, cols, ("credit",)))
                skip(row, kind.value)
            case _:
                skip(row, kind.value)
    _read_summary_footer(footer, out)
    return out


def _read_summary_footer(footer: list[RawRow], out: SortedRows) -> None:
    """A summary footer is a row of labels ('Opening Balance', 'Debits', ...) followed by a row of values."""
    for labels, values in zip(footer, footer[1:], strict=False):
        positions = {_summary_label(v): i for i, v in enumerate(labels.values) if _summary_label(v)}
        if not positions:
            continue
        found = {key: _amount_at(values, i) for key, i in positions.items()}
        out.opening = out.opening if out.opening is not None else found.get("opening")
        out.closing = out.closing if out.closing is not None else found.get("closing")
        if "debits" in found or "credits" in found:
            out.footer_totals = (found.get("debits"), found.get("credits"))
        return


def _summary_label(value) -> str | None:
    text = normalize_text(value)
    if text.startswith("OPENING"):
        return "opening"
    if text.startswith("CLOSING"):
        return "closing"
    if text in ("DEBITS", "TOTAL DEBITS", "WITHDRAWALS", "TOTAL WITHDRAWALS"):
        return "debits"
    if text in ("CREDITS", "TOTAL CREDITS", "DEPOSITS", "TOTAL DEPOSITS"):
        return "credits"
    return None


def _amount_at(row: RawRow, index: int) -> Decimal | None:
    try:
        amount = parse_amount(row.get(index))
    except CleanError:
        return None
    return amount.value if amount else None


@dataclass(frozen=True)
class DateFormats:
    txn_date: str | None
    value_date: str | None
    use_value_date: bool


def _infer_date_formats(candidates: list[RawRow], header: HeaderMatch, issues: list[Issue],
                        file_name: str) -> DateFormats:
    cols = header.columns
    txn_format = infer_date_format((r.get(cols["txn_date"]) for r in candidates), header.date_formats)
    if "value_date" not in cols:
        return DateFormats(txn_format, None, use_value_date=False)
    try:
        value_format = infer_date_format((r.get(cols["value_date"]) for r in candidates),
                                         [f for f in (txn_format, *header.date_formats) if f])
    except ValueError as e:
        issues.append(Issue(Severity.WARNING, file_name, None, "VALUE_DATE_FORMAT", f"Value Date column ignored: {e}"))
        return DateFormats(txn_format, None, use_value_date=False)
    return DateFormats(txn_format, value_format, use_value_date=True)


def _parse_candidates(sorted_rows: SortedRows, header: HeaderMatch, style: AmountStyle, formats: DateFormats,
                      file_name: str) -> tuple[list[BankTxn], list[RejectedRow]]:
    txns: list[BankTxn] = []
    rejected: list[RejectedRow] = []
    for row in sorted_rows.candidates:
        try:
            txn = _parse_txn(row, header, style, formats)
        except CleanError as e:
            amount, direction = _readable_amount(row, header, style)
            rejected.append(RejectedRow(file_name, row.row_number, f"{e.code}: {e.message}", raw_dict(row, header),
                                        amount, direction))
            continue
        if extra := sorted_rows.continued_narration.get(row.row_number):
            txn.narration = " ".join([txn.narration, *extra]).strip()
        txns.append(txn)
    return txns, rejected


def _amount_style(cols: dict[str, int]) -> AmountStyle:
    if "debit" in cols and "credit" in cols:
        return AmountStyle.SPLIT
    if "amount" in cols and "dr_cr" in cols:
        return AmountStyle.INDICATOR
    return AmountStyle.SIGNED


def _parse_txn(row: RawRow, header: HeaderMatch, style: AmountStyle, formats: DateFormats) -> BankTxn:
    cols = header.columns
    warnings: list[str] = []
    txn_date, w = parse_date(row.get(cols["txn_date"]), formats.txn_date)
    if txn_date is None:
        raise CleanError("MISSING_DATE", "Row has an amount but no transaction date")
    if w:
        warnings.append(f"Txn Date: {w}")

    direction, amount, w = _direction_and_amount(row, header, style)
    if w:
        warnings.append(w)

    value_date = None
    if formats.use_value_date:
        try:
            value_date, w = parse_date(row.get(cols["value_date"]), formats.value_date)
            if w:
                warnings.append(f"Value Date: {w}")
        except CleanError as e:
            warnings.append(f"Value Date ignored: {e.message}")
        if value_date and abs((value_date - txn_date).days) > VALUE_DATE_TOLERANCE_DAYS:
            warnings.append(f"Value date {value_date} is more than {VALUE_DATE_TOLERANCE_DAYS} days from "
                            f"transaction date {txn_date}")

    balance = None
    if "balance" in cols:
        try:
            b = parse_amount(row.get(cols["balance"]))
            if b is not None:
                balance = -abs(b.value) if b.marker == "DR" else b.value
        except CleanError as e:
            warnings.append(f"Balance ignored: {e.message}")

    return BankTxn(
        source_row=row.row_number,
        txn_date=txn_date,
        value_date=value_date,
        narration=as_text(row.get(cols["narration"])),
        reference=as_text(row.get(cols.get("reference"))),
        direction=direction,
        amount=amount,
        balance=balance,
        warnings=warnings,
    )


def _direction_and_amount(row: RawRow, header: HeaderMatch, style: AmountStyle) -> tuple[Direction, Decimal, str | None]:
    """Returns (direction, positive amount, warning)."""
    cols = header.columns
    if style is AmountStyle.SPLIT:
        d = parse_amount(row.get(cols["debit"]))
        c = parse_amount(row.get(cols["credit"]))
        if d and d.marker == "CR":
            raise CleanError("CONFLICTING_DIRECTION", "Debit column value is marked 'Cr'")
        if c and c.marker == "DR":
            raise CleanError("CONFLICTING_DIRECTION", "Credit column value is marked 'Dr'")
        d_val = d.value if d else ZERO
        c_val = c.value if c else ZERO
        if d_val and c_val:
            raise CleanError("BOTH_DEBIT_AND_CREDIT", f"Row has both a debit ({d_val}) and a credit ({c_val})")
        if not d_val and not c_val:
            raise CleanError("NO_AMOUNT", "Row has neither a debit nor a credit amount")
        if d_val:
            warn = f"Negative value {d_val} in Debit column read as a debit of {abs(d_val)}" if d_val < 0 else None
            return Direction.DEBIT, abs(d_val), warn
        warn = f"Negative value {c_val} in Credit column read as a credit of {abs(c_val)}" if c_val < 0 else None
        return Direction.CREDIT, abs(c_val), warn

    a = parse_amount(row.get(cols["amount"]))
    if a is None or a.value == 0:
        raise CleanError("NO_AMOUNT", "Row has no amount")

    if style is AmountStyle.INDICATOR:
        word = normalize_text(row.get(cols["dr_cr"]))
        if word in DEBIT_WORDS:
            direction = Direction.DEBIT
        elif word in CREDIT_WORDS:
            direction = Direction.CREDIT
        else:
            raise CleanError("UNKNOWN_DR_CR", f"Debit/credit indicator '{as_text(row.get(cols['dr_cr']))}' is not recognised")
        if a.marker and a.marker != word[:2]:
            raise CleanError("CONFLICTING_DIRECTION", f"Amount marked '{a.marker}' but indicator says '{word}'")
        return direction, abs(a.value), None

    if a.marker:
        if a.value < 0:
            raise CleanError("CONFLICTING_DIRECTION", f"Amount '{as_text(row.get(cols['amount']))}' has both a sign and Dr/Cr")
        return (Direction.DEBIT if a.marker == "DR" else Direction.CREDIT), a.value, None
    negative_means = Direction.CREDIT if header.negative_amount_means == "CREDIT" else Direction.DEBIT
    other = Direction.CREDIT if negative_means is Direction.DEBIT else Direction.DEBIT
    return (negative_means if a.value < 0 else other), abs(a.value), None


def _readable_amount(row: RawRow, header: HeaderMatch, style: AmountStyle) -> tuple[Decimal | None, Direction | None]:
    try:
        direction, amount, _ = _direction_and_amount(row, header, style)
        return amount, direction
    except CleanError:
        return None, None


def _check_balances(txns: list[BankTxn], rejected: list[RejectedRow], opening: Decimal | None,
                    closing: Decimal | None, issues: list[Issue], file_name: str) -> BalanceCheck:
    with_balance = [t for t in txns if t.balance is not None]
    if not with_balance:
        msg = "The statement has no usable Balance column, so the parse cannot be cross-checked against it."
        issues.append(Issue(Severity.WARNING, file_name, None, "BALANCE_NOT_VERIFIABLE", msg))
        return BalanceCheck(BalanceStatus.NOT_VERIFIABLE, opening, closing, 0, msg)

    seq = list(txns)
    if len(seq) > 1 and seq[0].txn_date > seq[-1].txn_date:
        seq.reverse()

    breaks = explained = 0
    prev_balance, prev_row = opening, None
    for t in seq:
        if t.balance is None:
            prev_balance = None
            continue
        if prev_balance is not None:
            expected = prev_balance - t.debit + t.credit
            gap = t.balance - expected
            if gap:
                skipped = _rejected_between(rejected, prev_row, t.source_row)
                if skipped and _net_effect(skipped) == gap:
                    explained += 1
                    rows = ", ".join(str(r.source_row) for r in skipped)
                    issues.append(Issue(Severity.WARNING, file_name, t.source_row, "BALANCE_GAP_UNREADABLE_ROW",
                                        f"The balance moves by {gap:,.2f} more than the rows show because row(s) "
                                        f"{rows} just above could not be read. Nothing is missing.", gap))
                else:
                    breaks += 1
                    issues.append(Issue(Severity.ERROR, file_name, t.source_row, "BALANCE_BREAK",
                                        f"The bank's running balance changes by {gap:,.2f} more than the rows explain: "
                                        f"after row {t.source_row} it should be {expected:,.2f}, but the statement says "
                                        f"{t.balance:,.2f}. A row is probably missing just before row {t.source_row}.",
                                        gap))
        prev_balance, prev_row = t.balance, t.source_row
    if closing is not None and prev_balance is not None and prev_balance != closing:
        breaks += 1
        issues.append(Issue(Severity.ERROR, file_name, None, "CLOSING_BALANCE_MISMATCH",
                            f"The last running balance {prev_balance:,.2f} does not equal the closing balance "
                            f"{closing:,.2f} printed on the statement.", closing - prev_balance))

    if breaks:
        return BalanceCheck(BalanceStatus.BROKEN, opening, closing, breaks,
                            f"{breaks} unexplained break(s) in the running balance. Totals from this file are not proven.")
    msg = "Every row's running balance agrees with its debit/credit"
    if explained:
        msg += f", apart from {explained} gap(s) caused by rows that could not be read"
    msg += " (some rows have no balance and were skipped)." if len(with_balance) < len(txns) else "."
    if closing is not None:
        msg += " Closing balance agrees."
    return BalanceCheck(BalanceStatus.VERIFIED, opening, closing, 0, msg)


def _rejected_between(rejected: list[RejectedRow], row_a: int | None, row_b: int) -> list[RejectedRow]:
    low, high = sorted((row_a or 0, row_b))
    return [r for r in rejected if low < r.source_row < high]


def _net_effect(rows: list[RejectedRow]) -> Decimal | None:
    """What these unreadable rows would have done to the balance, if all their amounts are readable."""
    if any(r.amount is None or r.direction is None for r in rows):
        return None
    return sum((r.amount if r.direction is Direction.CREDIT else -r.amount for r in rows), ZERO)


def _check_footer_totals(txns: list[BankTxn], totals: tuple[Decimal | None, Decimal | None],
                         issues: list[Issue], file_name: str) -> None:
    debit_total, credit_total = totals
    sum_d = sum((t.debit for t in txns), ZERO)
    sum_c = sum((t.credit for t in txns), ZERO)
    for label, stated, actual in (("debits", debit_total, sum_d), ("credits", credit_total, sum_c)):
        if stated is not None and stated != actual:
            issues.append(Issue(Severity.ERROR, file_name, None, "FOOTER_TOTAL_MISMATCH",
                                f"Statement footer says total {label} = {stated:,.2f}; parsed rows add up to {actual:,.2f}."))


def _drop_repeated_export_lines(txns: list[BankTxn], sorted_rows: SortedRows, issues: list[Issue], file_name: str,
                                header: HeaderMatch) -> list[BankTxn]:
    """A line identical to an earlier one INCLUDING its running balance is the export repeating itself: a second real
    payment would have moved the balance. Lines identical except for the balance are real and only flagged."""
    raw_rows = {r.row_number: r for r in sorted_rows.candidates}
    first_seen: dict[tuple, int] = {}
    seen_without_balance: set[tuple] = set()
    kept = []
    for t in txns:
        key = (t.txn_date, t.direction, t.amount, t.reference.upper(), normalize_text(t.narration))
        earlier = first_seen.get((*key, t.balance)) if t.balance is not None else None
        if earlier is not None:
            message = (f"Row {t.source_row} is an exact copy of row {earlier}, including the running balance: the export "
                       f"repeated the line. The bank paid {t.amount:,.2f} only once.")
            sorted_rows.noise.append(NoiseRow(file_name, t.source_row, message, raw_dict(raw_rows[t.source_row], header)))
            issues.append(Issue(Severity.WARNING, file_name, t.source_row, "REPEATED_EXPORT_LINE", message, t.amount))
            continue
        if key in seen_without_balance:
            issues.append(Issue(Severity.WARNING, file_name, t.source_row, "DUPLICATE_BANK_ROW",
                                "Same date, amount, reference and narration as an earlier row, but the running balance "
                                "moved again: the bank really did pay twice."))
        first_seen[(*key, t.balance)] = t.source_row
        seen_without_balance.add(key)
        kept.append(t)
    return kept


def _first_amount(row: RawRow, cols: dict[str, int], fields: tuple[str, ...]) -> Decimal | None:
    for f in fields:
        if f in cols:
            try:
                a = parse_amount(row.get(cols[f]))
            except CleanError:
                continue
            if a is not None:
                return -abs(a.value) if a.marker == "DR" and f == "balance" else a.value
    return None


def _fmt(v: Decimal | None) -> str:
    return f"{v:,.2f}" if v is not None else "no amount"
