"""Canonical data model.

Everything after parsing works only with these types. The matcher never sees
Excel column names or raw strings, so a new bank format never touches it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum, StrEnum
from typing import Any


class Direction(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class PayoutStatus(str, Enum):
    PAID = "PAID"
    PENDING = "PENDING"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REVERSED = "REVERSED"


class Severity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


class BalanceStatus(StrEnum):
    VERIFIED = "VERIFIED"  # every running balance follows from the parsed debits/credits
    BROKEN = "BROKEN"  # at least one break: a row is missing, duplicated or misread
    NOT_VERIFIABLE = "NOT_VERIFIABLE"  # the statement has no usable balance column


class AmountStyle(StrEnum):
    SPLIT = "split"  # separate debit and credit columns
    SIGNED = "signed"  # one amount column; the sign (or a Dr/Cr suffix) gives the direction
    INDICATOR = "indicator"  # one amount column plus a Dr/Cr column


class TxnCategory(str, Enum):
    PAYOUT_CANDIDATE = "Payout-like transfer"
    BANK_CHARGE = "Bank charge"
    NON_PAYOUT = "Non-payout debit"
    OTHER_DEBIT = "Other debit"
    REVERSAL = "Return / reversal credit"
    OTHER_CREDIT = "Other credit"


@dataclass
class Payout:
    payout_id: str
    employee_id: str
    employee_name: str
    account_masked: str
    account_last4: str | None
    ifsc: str | None
    amount: Decimal
    payout_date: date
    claim_ids: list[str]
    status: PayoutStatus
    raw_status: str
    source_row: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class BankTxn:
    source_row: int
    txn_date: date
    value_date: date | None
    narration: str
    reference: str
    direction: Direction
    amount: Decimal  # always positive; direction says which way
    balance: Decimal | None
    warnings: list[str] = field(default_factory=list)

    @property
    def debit(self) -> Decimal:
        return self.amount if self.direction is Direction.DEBIT else Decimal("0")

    @property
    def credit(self) -> Decimal:
        return self.amount if self.direction is Direction.CREDIT else Decimal("0")


@dataclass
class RejectedRow:
    source_file: str
    source_row: int
    reason: str
    raw: dict[str, Any]  # header text -> cell text, in column order
    amount: Decimal | None = None
    direction: Direction | None = None
    record_id: str = ""

    @property
    def message(self) -> str:
        return self.reason.split(": ", 1)[-1]


@dataclass
class NoiseRow:
    source_file: str
    source_row: int
    reason: str
    raw: dict[str, Any]


@dataclass
class Issue:
    severity: Severity
    source_file: str
    source_row: int | None
    code: str
    message: str
    amount: Decimal | None = None  # for a balance gap: how much money the rows do not explain


class RowAccountingError(RuntimeError):
    """Rows were lost or double-counted while parsing. Always a bug, never bad input."""


@dataclass
class RowAccounting:
    total_rows: int = 0
    preamble: int = 0
    header: int = 0
    noise: int = 0
    parsed: int = 0
    rejected: int = 0

    @property
    def balanced(self) -> bool:
        return self.total_rows == self.preamble + self.header + self.noise + self.parsed + self.rejected

    def ensure_balanced(self, file_name: str) -> None:
        # An explicit check, not `assert`: asserts disappear under `python -O`.
        if not self.balanced:
            raise RowAccountingError(f"Row accounting does not add up for {file_name}: {self}")


@dataclass
class LayoutInfo:
    id: str
    source: str
    verified: bool
    llm_called: bool
    attempts: int
    notes: str
    problems: list[str]


@dataclass
class PayoutParseResult:
    file_name: str
    sheet: str
    header_row: int
    rows: list[Payout]
    rejected: list[RejectedRow]
    noise: list[NoiseRow]
    issues: list[Issue]
    accounting: RowAccounting
    date_format: str | None
    control_total: Decimal | None = None
    layout: LayoutInfo | None = None


@dataclass
class BalanceCheck:
    status: BalanceStatus
    opening: Decimal | None
    closing: Decimal | None
    breaks: int
    message: str


@dataclass
class BankParseResult:
    file_name: str
    sheet: str
    header_row: int
    rows: list[BankTxn]
    rejected: list[RejectedRow]
    noise: list[NoiseRow]
    issues: list[Issue]
    accounting: RowAccounting
    date_format: str | None
    amount_style: AmountStyle
    balance_check: BalanceCheck
    layout: LayoutInfo | None = None
