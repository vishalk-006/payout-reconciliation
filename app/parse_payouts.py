"""File A: Payout Register (from Chronon). Trusted source, but still validated:
it is an Excel export that a person may have opened and edited."""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from .cleaners import CleanError, as_text, infer_date_format, is_blank, normalize_text, parse_amount, parse_date
from .models import Issue, NoiseRow, Payout, PayoutParseResult, PayoutStatus, RejectedRow, RowAccounting, Severity
from .parse_common import raw_dict, unread_sheet_issues
from .layout.models import HeaderMatch, is_header_like
from .layout.providers import LayoutDetector
from .layout.resolver import Check, resolve_and_parse
from .layout.spec import PAYOUT_REGISTER
from .layout.store import LayoutStore
from .reader import RawRow, read_sheets

STATUS_ALIASES: dict[str, PayoutStatus] = {
    **dict.fromkeys(["PAID", "SUCCESS", "SUCCESSFUL", "COMPLETED", "COMPLETE", "PROCESSED", "SETTLED",
                     "DISBURSED", "CREDITED"], PayoutStatus.PAID),
    **dict.fromkeys(["PENDING", "PROCESSING", "IN PROGRESS", "INITIATED", "QUEUED", "SUBMITTED",
                     "SCHEDULED"], PayoutStatus.PENDING),
    **dict.fromkeys(["FAILED", "FAILURE", "REJECTED", "DECLINED", "ERROR"], PayoutStatus.FAILED),
    **dict.fromkeys(["CANCELLED", "CANCELED", "VOID", "VOIDED"], PayoutStatus.CANCELLED),
    **dict.fromkeys(["REVERSED", "RETURNED", "REFUNDED"], PayoutStatus.REVERSED),
}
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
CLAIM_SPLIT_RE = re.compile(r"[,;|\n/]+")
MAX_REJECTED_SHARE = 0.25  # a person may have edited the register; more than this points at a wrong layout


def parse_payout_register(path: Path, file_name: str | None = None, *, store: LayoutStore | None = None,
                          detector: LayoutDetector | None = None, provider: str | None = None) -> PayoutParseResult:
    file_name = file_name or path.name
    return resolve_and_parse(read_sheets(path), PAYOUT_REGISTER, file_name,
                             lambda sheets, header: parse_payout_rows(sheets, header, file_name),
                             check_payout_parse, store=store, detector=detector, provider=provider)


def check_payout_parse(res: PayoutParseResult) -> Check:
    """The register has no balance chain, so nothing in the file proves e.g. that two ID columns were not swapped:
    a new register layout is always held for human review (proven=False)."""
    total = len(res.rows) + len(res.rejected)
    if not res.rows:
        return Check(["No payout rows could be read with this layout."], fatal=True, proven=False)
    share = len(res.rejected) / total
    if share > MAX_REJECTED_SHARE:
        sample = "; ".join(f"row {r.source_row}: {r.reason}" for r in res.rejected[:3])
        return Check([f"{len(res.rejected)} of {total} payout rows could not be read ({sample})."],
                     fatal=share > 0.5, proven=False)
    return Check(proven=False)


def parse_payout_rows(sheets: dict[str, list[RawRow]], header: HeaderMatch, file_name: str) -> PayoutParseResult:
    rows = sheets[header.sheet]
    data_rows = rows[header.row_index + 1:]
    cols = header.columns

    acc = RowAccounting(total_rows=len(rows), preamble=header.row_index, header=1)
    issues: list[Issue] = []
    noise: list[NoiseRow] = []
    rejected: list[RejectedRow] = []
    payouts: list[Payout] = []
    control_total: Decimal | None = None

    date_fmt = infer_date_format((r.get(cols["payout_date"]) for r in data_rows), header.date_formats)
    first_seen: dict[str, int] = {}

    for row in data_rows:
        raw = raw_dict(row, header)
        if all(is_blank(v) for v in row.values):
            noise.append(NoiseRow(file_name, row.row_number, "Blank row", raw))
            continue
        if is_header_like(row, header):
            noise.append(NoiseRow(file_name, row.row_number, "Repeated header row", raw))
            continue
        if is_blank(row.get(cols["payout_date"])) and _looks_like_total(row):
            noise.append(NoiseRow(file_name, row.row_number, "Total row (used as a control total)", raw))
            try:
                amt = parse_amount(row.get(cols["amount"]))
                control_total = amt.value if amt else control_total
            except CleanError:
                issues.append(Issue(Severity.WARNING, file_name, row.row_number, "BAD_TOTAL_ROW",
                                    "Total row amount could not be read"))
            continue

        try:
            payout = _parse_row(row, cols, date_fmt)
        except CleanError as e:
            rejected.append(RejectedRow(file_name, row.row_number, f"{e.code}: {e.message}", raw,
                                        _readable_amount(row, cols), record_id=as_text(row.get(cols["payout_id"]))))
            continue

        key = payout.payout_id.upper()
        if key in first_seen:
            rejected.append(RejectedRow(
                file_name, row.row_number,
                f"DUPLICATE_PAYOUT_ID: Payout ID {payout.payout_id} already appears on row {first_seen[key]}",
                raw, payout.amount, record_id=payout.payout_id))
            continue
        first_seen[key] = row.row_number
        payouts.append(payout)

    acc.noise, acc.parsed, acc.rejected = len(noise), len(payouts), len(rejected)
    acc.ensure_balanced(file_name)
    issues.extend(unread_sheet_issues(sheets, header, file_name))

    if control_total is not None:
        parsed_total = sum((p.amount for p in payouts), Decimal("0")) + sum(
            (r.amount for r in rejected if r.amount), Decimal("0"))
        if parsed_total != control_total:
            issues.append(Issue(Severity.ERROR, file_name, None, "CONTROL_TOTAL_MISMATCH",
                                f"Total row says {control_total:,.2f} but the rows add up to {parsed_total:,.2f}"))
    for p in payouts:
        for w in p.warnings:
            issues.append(Issue(Severity.WARNING, file_name, p.source_row, "PAYOUT_WARNING", f"{p.payout_id}: {w}"))

    return PayoutParseResult(file_name, header.sheet, header.row_number, payouts, rejected, noise, issues,
                             acc, date_fmt, control_total)


def _parse_row(row: RawRow, cols: dict[str, int], date_fmt: str | None) -> Payout:
    warnings: list[str] = []
    payout_id = as_text(row.get(cols["payout_id"]))
    if not payout_id:
        raise CleanError("MISSING_PAYOUT_ID", "Payout ID is empty")

    amt = parse_amount(row.get(cols["amount"]))
    if amt is None:
        raise CleanError("MISSING_AMOUNT", "Amount is empty")
    if amt.value <= 0:
        raise CleanError("NON_POSITIVE_AMOUNT", f"Amount {amt.value} must be greater than zero")

    payout_date, warn = parse_date(row.get(cols["payout_date"]), date_fmt)
    if payout_date is None:
        raise CleanError("MISSING_DATE", "Payout Date is empty")
    if warn:
        warnings.append(warn)

    raw_status = as_text(row.get(cols["status"]))
    status = STATUS_ALIASES.get(normalize_text(raw_status))
    if status is None:
        raise CleanError("UNKNOWN_STATUS", f"Status '{raw_status}' is not a known payout status")

    account = as_text(row.get(cols.get("bank_account")))
    digits = re.sub(r"\D", "", account)
    if account and len(digits) < 4:
        warnings.append(f"Bank account '{account}' shows fewer than 4 digits; cannot use it for matching")

    ifsc = as_text(row.get(cols.get("ifsc"))).upper() or None
    if ifsc and not IFSC_RE.match(ifsc):
        warnings.append(f"IFSC '{ifsc}' is not a valid IFSC format")

    claims_raw = as_text(row.get(cols.get("claim_ids")))
    claim_ids = [c.strip() for c in CLAIM_SPLIT_RE.split(claims_raw) if c.strip()]

    return Payout(
        payout_id=payout_id,
        employee_id=as_text(row.get(cols.get("employee_id"))),
        employee_name=as_text(row.get(cols.get("employee_name"))),
        account_masked=account,
        account_last4=digits[-4:] if len(digits) >= 4 else None,
        ifsc=ifsc,
        amount=amt.value,
        payout_date=payout_date,
        claim_ids=claim_ids,
        status=status,
        raw_status=raw_status,
        source_row=row.row_number,
        warnings=warnings,
    )


def _looks_like_total(row: RawRow) -> bool:
    return any(isinstance(v, str) and normalize_text(v).startswith(("TOTAL", "GRAND TOTAL")) for v in row.values)


def _readable_amount(row: RawRow, cols: dict[str, int]) -> Decimal | None:
    try:
        amount = parse_amount(row.get(cols["amount"]))
    except CleanError:
        return None
    return amount.value if amount and amount.value > 0 else None
