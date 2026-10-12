"""What each kind of input file must contain, in our own vocabulary.

No bank-specific column names live here: mapping a file's headers onto these
fields is the job of the layout detector (an LLM) and the layout cache.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    description: str


@dataclass(frozen=True)
class KindSpec:
    kind: str
    title: str
    fields: tuple[FieldSpec, ...]
    required: frozenset[str]
    required_any: tuple[frozenset[str], ...] = ()

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def missing(self, present: set[str]) -> list[str]:
        out = [f for f in sorted(self.required) if f not in present]
        if self.required_any and not any(group <= present for group in self.required_any):
            out.append(" or ".join("+".join(sorted(g)) for g in self.required_any))
        return out


BANK_STATEMENT = KindSpec(
    kind="bank_statement",
    title="bank account statement",
    fields=(
        FieldSpec("txn_date", "Transaction or posting date of the row."),
        FieldSpec("value_date", "Value date, only if the file has a separate value-date column."),
        FieldSpec("narration", "Transaction description / particulars / remarks text."),
        FieldSpec("reference", "Cheque number, reference number or UTR."),
        FieldSpec("debit", "Money going OUT (withdrawals), when debits and credits are in separate columns."),
        FieldSpec("credit", "Money coming IN (deposits), when debits and credits are in separate columns."),
        FieldSpec("amount", "A single amount column, ONLY when there are no separate debit and credit columns."),
        FieldSpec("dr_cr", "Column saying whether a single amount is a debit or a credit (values like Dr/Cr)."),
        FieldSpec("balance", "Running balance after each row."),
    ),
    required=frozenset({"txn_date", "narration"}),
    required_any=(frozenset({"debit", "credit"}), frozenset({"amount"})),
)

PAYOUT_REGISTER = KindSpec(
    kind="payout_register",
    title="employee reimbursement payout register",
    fields=(
        FieldSpec("payout_id", "Unique payout identifier."),
        FieldSpec("employee_id", "Employee identifier or code."),
        FieldSpec("employee_name", "Employee / beneficiary name."),
        FieldSpec("bank_account", "Employee bank account number (usually masked)."),
        FieldSpec("ifsc", "Bank IFSC code."),
        FieldSpec("amount", "Payout amount."),
        FieldSpec("payout_date", "Date the payout was made or initiated."),
        FieldSpec("claim_ids", "Expense claim IDs covered by the payout."),
        FieldSpec("status", "Payout status (paid, pending, failed...)."),
    ),
    required=frozenset({"payout_id", "amount", "payout_date", "status"}),
)

KINDS: dict[str, KindSpec] = {k.kind: k for k in (BANK_STATEMENT, PAYOUT_REGISTER)}
