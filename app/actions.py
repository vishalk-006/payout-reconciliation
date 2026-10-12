"""Turn reconciliation results into a to-do list a finance person can act on without
knowing how the matching works: what to do, for whom, how much money, and the proof."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

from .classify import joined_words
from .cleaners import compact
from .layout.models import SEEDED
from .matching import BankBucket, PayoutBucket, PayoutResult, ReconResult, TxnResult, day, money
from .models import BankParseResult, Issue, PayoutParseResult, PayoutStatus, RejectedRow

ZERO = Decimal("0")
FILE_INTEGRITY_CODES = ("BALANCE_BREAK", "CLOSING_BALANCE_MISMATCH", "FOOTER_TOTAL_MISMATCH", "UNREAD_SHEET",
                        "CONTROL_TOTAL_MISMATCH", "REPEATED_EXPORT_LINE")


class Action(Enum):
    """In the order they appear on the Action List: money first, then data, then confirmations."""
    PAY_AGAIN = "Pay these employees again – they did not receive the money"
    RECOVER = "Recover money – paid too much or more than once"
    INVESTIGATE = "Find out what this money is – it moved in the bank, but no payout explains it"
    FIX_FILE = "Fix the files – these rows could not be read or the file does not add up"
    ASK_BANK = "Ask the bank – Chronon says paid, but the bank statement does not show it"
    PAY_DIFFERENCE = "Pay the difference – paid less than approved"
    DO_NOT_REPAY = "Do NOT pay again – marked failed in Chronon, but the bank did pay"
    UPDATE_STATUS = "Update the status in Chronon"
    CONFIRM = "Confirm these matches – probably right, but not proven"
    WAIT = "Nothing to do yet – check again on the next statement"


@dataclass
class ActionItem:
    action: Action
    employee: str
    payout_id: str
    amount: Decimal | None  # the money to act on: pay, recover or investigate
    what_happened: str
    next_step: str
    evidence: str


@dataclass
class FileEvidence:
    """Problems in the bank file that explain a payout the statement seems to be missing."""
    unreadable_row_of: dict[str, RejectedRow]  # payout id -> the unreadable bank row that mentions it
    gap_of_amount: dict[Decimal, Issue]  # payout amount -> a balance gap of exactly that size (debit dropped)
    used: set[int]  # id() of rows and issues already explained by a payout item

    @classmethod
    def collect(cls, bank: BankParseResult, result: ReconResult) -> FileEvidence:
        ids = {compact(r.payout.payout_id): r.payout.payout_id for r in result.payouts}
        unreadable = {}
        for rj in bank.rejected:
            mentioned = {ids[w] for w in joined_words(" ".join(rj.raw.values())) if w in ids}
            if len(mentioned) == 1:
                unreadable[mentioned.pop()] = rj
        gaps: dict[Decimal, list[Issue]] = {}
        for issue in bank.issues:
            if issue.code == "BALANCE_BREAK" and issue.amount is not None and issue.amount < 0:
                gaps.setdefault(-issue.amount, []).append(issue)
        return cls(unreadable, {amount: found[0] for amount, found in gaps.items() if len(found) == 1}, set())

    def explain_missing(self, r: PayoutResult) -> ActionItem | None:
        p = r.payout
        if (rj := self.unreadable_row_of.get(p.payout_id)) is not None:
            self.used.add(id(rj))
            gap = self.gap_of_amount.get(p.amount)
            gap_note = ""
            if gap is not None and gap.source_row > rj.source_row:
                self.used.add(id(self.gap_of_amount.pop(p.amount)))
                gap_note = f" The balance gap of {money(p.amount)} at row {gap.source_row} is this same payment."
            return ActionItem(Action.FIX_FILE, p.employee_name, p.payout_id, p.amount,
                              f"Its bank row ({rj.source_file} row {rj.source_row}) is on the statement but could not be "
                              f"read: {rj.message}. The bank most likely paid it.{gap_note}",
                              f"Correct row {rj.source_row} in the bank file and upload again. Do NOT pay again.",
                              f"Register row {p.source_row}; bank row {rj.source_row}")
        if (gap := self.gap_of_amount.pop(p.amount, None)) is not None:
            self.used.add(id(gap))
            return ActionItem(Action.FIX_FILE, p.employee_name, p.payout_id, p.amount,
                              f"There is no bank row for it, but the statement's running balance drops by exactly "
                              f"{money(p.amount)} at row {gap.source_row} with no row explaining it. The export most "
                              f"likely left this payment out.",
                              "Download the statement again from the bank to confirm. Do NOT pay again until then.",
                              f"Register row {p.source_row}; bank file around row {gap.source_row}")
        return None


def build_actions(payouts: PayoutParseResult, bank: BankParseResult, result: ReconResult) -> list[ActionItem]:
    txn_results = {t.txn.source_row: t for t in result.txns}
    evidence = FileEvidence.collect(bank, result)
    items = [item for r in result.payouts if (item := _payout_action(r, result, txn_results, evidence))]
    items += [item for t in result.txns if (item := _bank_action(t))]
    items += _file_actions(payouts, bank, evidence.used)
    order = list(Action)
    return sorted(items, key=lambda i: (order.index(i.action), i.payout_id, i.evidence))


def _payout_action(r: PayoutResult, result: ReconResult, txn_results: dict[int, TxnResult],
                   evidence: FileEvidence) -> ActionItem | None:
    p = r.payout
    if r.bucket is PayoutBucket.MISSING_IN_BANK and (explained := evidence.explain_missing(r)):
        return explained
    who, pid = p.employee_name, p.payout_id

    def item(action: Action, amount: Decimal | None, what: str, step: str) -> ActionItem:
        return ActionItem(action, who, pid, amount, what, step, _evidence(r))

    match r.bucket:
        case PayoutBucket.RETURNED:
            sent, back = r.txns[0], r.return_txn
            return item(Action.PAY_AGAIN, back.amount,
                        f"The bank sent {money(sent.amount)} on {day(sent.txn_date)}, but it came back on "
                        f"{day(back.txn_date)} (\"{back.narration}\").",
                        f"Correct {who}'s bank account / IFSC in Chronon, then pay {money(back.amount)} again.")
        case PayoutBucket.PAID_TWICE:
            extra = sum((t.amount for t in r.txns[1:]), ZERO)
            dates = " and ".join(f"{money(t.amount)} on {day(t.txn_date)}" for t in r.txns)
            return item(Action.RECOVER, extra, f"Paid {len(r.txns)} times: {dates}. Approved once for {money(p.amount)}.",
                        f"Ask {who} to return {money(extra)}, or ask the bank to recall the duplicate transfer.")
        case PayoutBucket.AMOUNT_MISMATCH:
            return _amount_mismatch(r, txn_results, item)
        case PayoutBucket.STATUS_CONFLICT:
            return _status_conflict(r, item)
        case PayoutBucket.MISSING_IN_BANK:
            if _may_be_in_transit(p.payout_date, result):
                return item(Action.WAIT, None,
                            f"Paid on {day(p.payout_date)}, close to the end of this statement "
                            f"({day(result.period[1])}). It has probably not reached the statement yet.",
                            "Check that it appears on the next statement.")
            return item(Action.ASK_BANK, p.amount,
                        f"Chronon says {money(p.amount)} was paid on {day(p.payout_date)}, but no matching debit is on "
                        f"the bank statement{_period(result)}.",
                        "Ask the bank / payment gateway for the transfer reference (UTR). If it was never sent, pay it; "
                        "do not pay again before checking.")
        case PayoutBucket.LOW_CONFIDENCE:
            t = r.txns[0]
            return item(Action.CONFIRM, None,
                        f"Probably paid by the bank debit of {money(t.amount)} on {day(t.txn_date)} (\"{t.narration}\"), "
                        f"but the bank's text has no payout ID or name to prove it.",
                        "Check the bank's payment advice or UTR. If it is this payment, nothing else to do.")
        case PayoutBucket.AMBIGUOUS:
            rows = " or ".join(f"row {t.source_row} ({money(t.amount)} on {day(t.txn_date)})" for t in r.ambiguous_candidates)
            return item(Action.CONFIRM, None,
                        f"More than one bank debit could be this payment: {rows}. Same amount, same days, "
                        f"nothing in the bank's text to tell them apart.",
                        "Use the UTR / payment advice to tell which bank row belongs to this payout.")
        case PayoutBucket.NOT_YET_DEBITED:
            return item(Action.WAIT, None, f"Still {p.raw_status} in Chronon and not on the bank statement yet.",
                        "Nothing to do now; it should appear once the payment is processed.")
    return None


def _amount_mismatch(r: PayoutResult, txn_results: dict[int, TxnResult], item) -> ActionItem:
    p, t = r.payout, r.txns[0]
    shared_with = [pid for pid in txn_results[t.source_row].payout_ids if pid != p.payout_id]
    if shared_with:
        return item(Action.ASK_BANK, None,
                    f"One bank transfer of {money(t.amount)} on {day(t.txn_date)} covers this payout and "
                    f"{', '.join(shared_with)}, but their amounts do not add up to it.",
                    "Ask the bank for the break-up of the combined transfer.")
    diff = t.amount - p.amount
    paid = f"The bank paid {money(t.amount)} on {day(t.txn_date)}; the approved payout was {money(p.amount)}"
    if diff > 0:
        return item(Action.RECOVER, diff, f"{paid}: {money(diff)} too much.",
                    f"Recover {money(diff)} from {p.employee_name}, or adjust it against their next claim.")
    return item(Action.PAY_DIFFERENCE, -diff, f"{paid}: {money(-diff)} too little.",
                f"Pay {p.employee_name} the remaining {money(-diff)}.")


def _status_conflict(r: PayoutResult, item) -> ActionItem:
    p, t = r.payout, r.txns[0]
    if p.status in (PayoutStatus.FAILED, PayoutStatus.CANCELLED):
        return item(Action.DO_NOT_REPAY, None,
                    f"Chronon says {p.raw_status}, but the bank paid {money(t.amount)} on {day(t.txn_date)}.",
                    "Do not pay again. Mark the payout as paid in Chronon.")
    if p.status is PayoutStatus.REVERSED:
        return item(Action.ASK_BANK, None,
                    f"Chronon says {p.raw_status}, but no money came back on this statement after the payment on "
                    f"{day(t.txn_date)}.",
                    "Ask the bank whether the reversal happened.")
    return item(Action.UPDATE_STATUS, None,
                f"Chronon still says {p.raw_status}, but the bank paid {money(t.amount)} on {day(t.txn_date)}.",
                "Mark the payout as paid in Chronon.")


def _bank_action(t: TxnResult) -> ActionItem | None:
    txn = t.txn
    evidence = f"Bank row {txn.source_row}"
    if t.bucket is BankBucket.UNEXPLAINED:
        return ActionItem(Action.INVESTIGATE, "", "", txn.amount,
                          f"{money(txn.amount)} was paid out on {day(txn.txn_date)} (\"{txn.narration}\") and no payout "
                          f"in Chronon matches it.",
                          "Find who approved this transfer. If it is a reimbursement, check that it exists in Chronon; "
                          "otherwise treat it as unauthorised.", evidence)
    if t.bucket is BankBucket.RETURN_UNLINKED:
        return ActionItem(Action.INVESTIGATE, "", ", ".join(t.payout_ids), txn.amount,
                          f"{money(txn.amount)} came back on {day(txn.txn_date)} (\"{txn.narration}\"), but it is not "
                          f"clear which payment bounced.",
                          "Ask the bank which transfer this return belongs to, then pay that employee again.", evidence)
    return None


def _file_actions(payouts: PayoutParseResult, bank: BankParseResult, explained: set[int]) -> list[ActionItem]:
    items = []
    for parsed in (payouts, bank):
        items += [_unreadable_row(rj) for rj in parsed.rejected if id(rj) not in explained]
        items += [_file_issue(issue) for issue in parsed.issues
                  if issue.code in FILE_INTEGRITY_CODES and id(issue) not in explained]
        if parsed.layout and not parsed.layout.verified and parsed.layout.source != SEEDED:
            items.append(ActionItem(Action.FIX_FILE, "", "", None,
                                    f"The columns of {parsed.file_name} were identified automatically and could not be "
                                    f"fully proven: {' '.join(parsed.layout.problems)}",
                                    "Open 'All Bank Rows' / 'All Payouts' and check a few rows against the original "
                                    "file before acting on this report.", parsed.file_name))
    return items


def _file_issue(issue: Issue) -> ActionItem:
    where = f"{issue.source_file} row {issue.source_row}" if issue.source_row else issue.source_file
    if issue.code == "REPEATED_EXPORT_LINE":
        return ActionItem(Action.FIX_FILE, "", "", None, issue.message,
                          "Nothing to pay or recover. Re-download the statement if you need a clean copy.", where)
    return ActionItem(Action.FIX_FILE, "", "", None, issue.message,
                      "Download the file again from the source system and upload it again. Until then, the totals "
                      "from this file are not proven.", where)


def _unreadable_row(rj: RejectedRow) -> ActionItem:
    values = "; ".join(f"{k} = {v}" for k, v in rj.raw.items() if v)
    return ActionItem(Action.FIX_FILE, "", rj.record_id, rj.amount,
                      f"Row {rj.source_row} of {rj.source_file} could not be read: {rj.message}",
                      "Correct this row in the file and upload it again. Until then it is NOT reconciled.",
                      f"{rj.source_file} row {rj.source_row}: {values}")


def _evidence(r: PayoutResult) -> str:
    rows = [f"bank row {t.source_row}" for t in (r.txns or r.ambiguous_candidates)]
    if r.return_txn:
        rows.append(f"return on bank row {r.return_txn.source_row}")
    return f"Register row {r.payout.source_row}" + (f"; {', '.join(rows)}" if rows else "")


def _may_be_in_transit(paid_on: date, result: ReconResult) -> bool:
    return bool(result.period) and (result.period[1] - paid_on).days < result.config.in_transit_days


def _period(result: ReconResult) -> str:
    return f" ({day(result.period[0])} to {day(result.period[1])})" if result.period else ""


def headline(items: list[ActionItem], payout_count: int, ok_count: int) -> list[str]:
    """One line per kind of action, in plain words, for the top of the Action List."""
    lines = [f"{payout_count} payouts checked: {ok_count} were paid exactly as approved."]
    phrases = {
        Action.PAY_AGAIN: "employee(s) did NOT receive their money – {total} to pay again",
        Action.RECOVER: "case(s) of paying too much – {total} to recover",
        Action.INVESTIGATE: "bank movement(s) with no payout behind them – {total} to explain",
        Action.FIX_FILE: "problem(s) in the uploaded files to fix",
        Action.ASK_BANK: "payout(s) to check with the bank – {total}",
        Action.PAY_DIFFERENCE: "underpayment(s) – {total} still to pay",
        Action.DO_NOT_REPAY: "payout(s) marked failed that the bank DID pay – do not pay again",
        Action.UPDATE_STATUS: "status update(s) needed in Chronon",
        Action.CONFIRM: "probable match(es) to confirm",
        Action.WAIT: "payout(s) to check on the next statement",
    }
    for action, phrase in phrases.items():
        group = [i for i in items if i.action is action]
        if group:
            total = money(sum((i.amount for i in group if i.amount), ZERO))
            lines.append(f"• {len(group)} {phrase.format(total=total)}")
    if len(lines) == 1:
        lines.append("Nothing to do: every payout matches the bank and both files were read completely.")
    return lines
