"""Deterministic matching engine.

Rules run from strongest evidence to weakest. Once a payout or bank row is
claimed it leaves the pool. A weak rule only accepts a pair when the choice is
unique in BOTH directions; anything else is reported as ambiguous for a human.
Every outcome carries the rule that produced it and a plain-English reason.

  R1  Payout ID in narration/reference, amount equal
  R2  Payout ID in narration/reference, amount differs
  R3  Several Payout IDs in one bank debit (batch)
  R4  Amount equal + date window + account digits and/or name
  R5  Amount equal + date window only (unique both ways)        -> confirm
  R6  Account digits / full name + date window, amount differs  -> review
  R7  One bank debit = sum of same-day payouts                  -> confirm
  RT  Return credits linked back to the payout they reverse
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from collections.abc import Callable, Iterable

from .classify import (NAME_FULL, NAME_PARTIAL, Clues, account_evidence, classify, extract_clues,
                       name_evidence, name_tokens)
from .cleaners import compact
from .models import BankTxn, Direction, Payout, PayoutStatus, TxnCategory

ZERO = Decimal("0")


class PayoutBucket(str, Enum):
    MATCHED = "Matched"
    MATCHED_BATCH = "Matched – part of a combined bank debit"
    LOW_CONFIDENCE = "Probable match – please confirm"
    AMOUNT_MISMATCH = "Amount differs from bank"
    RETURNED = "Paid, then returned by bank"
    PAID_TWICE = "Paid more than once"
    STATUS_CONFLICT = "Register status disagrees with bank"
    AMBIGUOUS = "Several possible bank rows"
    MISSING_IN_BANK = "Marked paid – no bank debit found"
    RETURN_CONSISTENT = "Failed/reversed – debit returned (consistent)"
    NOT_YET_DEBITED = "Pending – not yet debited"
    NO_DEBIT_EXPECTED = "Failed/cancelled – no debit (as expected)"


class BankBucket(str, Enum):
    MATCHED = "Matched to a payout"
    MATCHED_REVIEW = "Linked to a payout – needs review"
    DUPLICATE_PAYMENT = "Duplicate payment of a payout"
    AMBIGUOUS = "Could belong to several payouts"
    UNEXPLAINED = "Payout-like debit with no payout"
    BANK_CHARGE = "Bank charges"
    NON_PAYOUT = "Not payout-related debit"
    RETURN_LINKED = "Return credit linked to a payout"
    RETURN_UNLINKED = "Return credit – no payout found"
    OTHER_CREDIT = "Other credit"


PAYOUT_REVIEW = {
    PayoutBucket.LOW_CONFIDENCE, PayoutBucket.AMOUNT_MISMATCH, PayoutBucket.RETURNED, PayoutBucket.PAID_TWICE,
    PayoutBucket.STATUS_CONFLICT, PayoutBucket.AMBIGUOUS, PayoutBucket.MISSING_IN_BANK,
}
BANK_REVIEW = {BankBucket.DUPLICATE_PAYMENT, BankBucket.AMBIGUOUS, BankBucket.UNEXPLAINED, BankBucket.RETURN_UNLINKED}
NO_DEBIT_STATUSES = {PayoutStatus.FAILED, PayoutStatus.CANCELLED}
MATCHABLE_CATEGORIES = (TxnCategory.PAYOUT_CANDIDATE, TxnCategory.OTHER_DEBIT)
SOME_EVIDENCE = 1  # e.g. a partial name
STRONG_EVIDENCE = 2  # account digits, or the full name


def _any_payout(p: Payout) -> bool:
    return True


def _expects_debit(p: Payout) -> bool:
    return p.status not in NO_DEBIT_STATUSES


def _no_evidence_needed(p: Payout, t: BankTxn) -> int:
    return 1


@dataclass(frozen=True)
class MatchConfig:
    days_before: int = 1  # the bank may book a debit slightly BEFORE the payout date
    days_after: int = 5
    return_window_days: int = 45
    min_id_length: int = 5  # shorter IDs ('101') are too likely to appear by coincidence
    in_transit_days: int = 3  # payouts this close to the statement end may simply not have cleared


@dataclass
class PayoutResult:
    payout: Payout
    bucket: PayoutBucket | None = None
    rule: str = ""
    confidence: str = ""
    reason: str = ""
    txns: list[BankTxn] = field(default_factory=list)  # primary debit first
    return_txn: BankTxn | None = None
    ambiguous_candidates: list[BankTxn] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return self.bucket in PAYOUT_REVIEW

    @property
    def bank_amount(self) -> Decimal | None:
        return self.txns[0].amount if self.txns else None


@dataclass
class TxnResult:
    txn: BankTxn
    category: TxnCategory
    category_reason: str
    bucket: BankBucket | None = None
    payout_ids: list[str] = field(default_factory=list)
    rule: str = ""
    reason: str = ""

    @property
    def needs_review(self) -> bool:
        return self.bucket in BANK_REVIEW


@dataclass
class ReconResult:
    payouts: list[PayoutResult]
    txns: list[TxnResult]
    config: MatchConfig
    period: tuple[date, date] | None


DEFAULT_CONFIG = MatchConfig()


def reconcile(payouts: list[Payout], txns: list[BankTxn], config: MatchConfig = DEFAULT_CONFIG) -> ReconResult:
    return _Matcher(payouts, txns, config).run()


def money(v: Decimal | None) -> str:
    return "—" if v is None else f"₹{v:,.2f}"


def day(d: date | None) -> str:
    return d.strftime("%d-%b-%Y") if d else "—"


def _lag(p: Payout, t: BankTxn) -> str:
    n = (t.txn_date - p.payout_date).days
    if n == 0:
        return "same day as the payout"
    return f"{abs(n)} day{'s' if abs(n) != 1 else ''} {'after' if n > 0 else 'before'} the payout date"


@dataclass
class DebitIndex:
    """Lookup tables over the open debits, so rules fetch candidates instead of scanning every row."""
    by_amount: dict[Decimal, list[BankTxn]]
    by_account: dict[str, list[BankTxn]]  # last 4 digits of any digit run in the narration
    by_word: dict[str, list[BankTxn]]

    @classmethod
    def build(cls, debits: list[BankTxn], clues: dict[int, Clues]) -> DebitIndex:
        by_amount, by_account, by_word = defaultdict(list), defaultdict(list), defaultdict(list)
        for t in debits:
            c = clues[t.source_row]
            by_amount[t.amount].append(t)
            for suffix in c.digit_suffixes:
                by_account[suffix].append(t)
            for word in set(c.words):
                by_word[word].append(t)
        return cls(by_amount, by_account, by_word)


class _Matcher:
    def __init__(self, payouts: list[Payout], txns: list[BankTxn], config: MatchConfig):
        self.cfg = config
        self.payouts = sorted(payouts, key=lambda p: (p.payout_date, p.payout_id))
        self.txns = sorted(txns, key=lambda t: (t.txn_date, t.source_row))
        self.clues: dict[int, Clues] = {t.source_row: extract_clues(t) for t in self.txns}

        id_index: dict[str, list[Payout]] = defaultdict(list)
        for p in self.payouts:
            key = compact(p.payout_id)
            if len(key) >= config.min_id_length:
                id_index[key].append(p)
        self.id_index = {k: v[0] for k, v in id_index.items() if len(v) == 1}
        self.mentions: dict[int, list[Payout]] = {
            t.source_row: sorted({self.id_index[k].payout_id: self.id_index[k]
                                  for k in self.clues[t.source_row].joined if k in self.id_index}.values(),
                                 key=lambda p: p.payout_id)
            for t in self.txns
        }

        self.p_res = {p.payout_id: PayoutResult(p) for p in self.payouts}
        self.t_res: dict[int, TxnResult] = {}
        for t in self.txns:
            category, why = classify(t, self.clues[t.source_row], bool(self.mentions[t.source_row]))
            self.t_res[t.source_row] = TxnResult(t, category, why)
        self.claimed_rows: set[int] = set()
        self._evidence_cache: dict[tuple[str, int], tuple[int, list[str]]] = {}
        self.ambig_p: dict[str, set[int]] = defaultdict(set)
        self.ambig_t: dict[int, set[str]] = defaultdict(set)
        self.period = (self.txns[0].txn_date, self.txns[-1].txn_date) if self.txns else None

    def run(self) -> ReconResult:
        self._match_by_id()
        self._match_by_attributes()
        self._match_batches_by_sum()
        self._link_returns()
        self._finalize()
        return ReconResult(list(self.p_res.values()), list(self.t_res.values()), self.cfg, self.period)

    def _open_payout(self, p: Payout) -> bool:
        return self.p_res[p.payout_id].bucket is None

    def _in_window(self, p: Payout, t: BankTxn) -> bool:
        lag = (t.txn_date - p.payout_date).days
        if -self.cfg.days_before <= lag <= self.cfg.days_after:
            return True
        if t.value_date:
            lag = (t.value_date - p.payout_date).days
            return -self.cfg.days_before <= lag <= self.cfg.days_after
        return False

    def _evidence(self, p: Payout, t: BankTxn) -> tuple[int, list[str]]:
        """Identity evidence linking a payout to a bank row: (score, plain-English reasons). Cached per pair."""
        key = (p.payout_id, t.source_row)
        if key not in self._evidence_cache:
            self._evidence_cache[key] = self._compute_evidence(p, t)
        return self._evidence_cache[key]

    def _compute_evidence(self, p: Payout, t: BankTxn) -> tuple[int, list[str]]:
        clues = self.clues[t.source_row]
        score, why = 0, []
        if account_evidence(p.account_last4, clues):
            score += STRONG_EVIDENCE
            why.append(f"account ending {p.account_last4} appears in the narration")
        n = name_evidence(p.employee_name, clues)
        if n == NAME_FULL:
            score += STRONG_EVIDENCE
            why.append(f"employee name '{p.employee_name}' appears in the narration")
        elif n == NAME_PARTIAL:
            score += SOME_EVIDENCE
            why.append(f"narration partly matches the name '{p.employee_name}'")
        return score, why

    def _claim(self, p: Payout, txns: list[BankTxn], bucket: PayoutBucket, rule: str, confidence: str,
               reason: str) -> None:
        r = self.p_res[p.payout_id]
        r.bucket, r.rule, r.confidence, r.reason, r.txns = bucket, rule, confidence, reason, list(txns)
        for t in txns:
            self.claimed_rows.add(t.source_row)
            tr = self.t_res[t.source_row]
            if p.payout_id not in tr.payout_ids:
                tr.payout_ids.append(p.payout_id)
            tr.rule = rule

    def _resolve(self, edges: dict[str, dict[int, int]]) -> list[tuple[str, int]]:
        """Accept a pair only if each side's best candidate is the other, uniquely."""
        best_t: dict[str, list[int]] = {}
        by_txn: dict[int, dict[str, int]] = defaultdict(dict)
        for pid, cands in edges.items():
            if not cands:
                continue
            top = max(cands.values())
            best_t[pid] = [row for row, s in cands.items() if s == top]
            for row, s in cands.items():
                by_txn[row][pid] = s
        accepted = []
        for pid, rows in best_t.items():
            if len(rows) == 1:
                row = rows[0]
                top = max(by_txn[row].values())
                if [q for q, s in by_txn[row].items() if s == top] == [pid]:
                    accepted.append((pid, row))
                    continue
            for row in edges[pid]:
                self.ambig_p[pid].add(row)
                self.ambig_t[row].add(pid)
        return accepted

    def _match_by_id(self) -> None:
        debits = [t for t in self.txns if t.direction is Direction.DEBIT and self.mentions[t.source_row]]

        for t in (t for t in debits if len(self.mentions[t.source_row]) >= 2):
            ps = self.mentions[t.source_row]
            total = sum((p.amount for p in ps), ZERO)
            ids = ", ".join(p.payout_id for p in ps)
            if total == t.amount:
                for p in ps:
                    self._claim(p, [t], PayoutBucket.MATCHED_BATCH, "R3 Batch: Payout IDs in one debit", "High",
                                f"Bank row {t.source_row} ({money(t.amount)} on {day(t.txn_date)}) lists Payout IDs "
                                f"{ids}; their amounts add up exactly to the debit.")
            else:
                for p in ps:
                    self._claim(p, [t], PayoutBucket.AMOUNT_MISMATCH, "R3 Batch: Payout IDs in one debit", "Medium",
                                f"Bank row {t.source_row} lists Payout IDs {ids}. They add up to {money(total)} but "
                                f"the bank debited {money(t.amount)} (difference {money(t.amount - total)}).")

        refs: dict[str, list[BankTxn]] = defaultdict(list)
        for t in debits:
            if t.source_row not in self.claimed_rows:
                refs[self.mentions[t.source_row][0].payout_id].append(t)

        for p in self.payouts:
            ts = refs.get(p.payout_id)
            if not ts:
                continue
            res = self.p_res[p.payout_id]
            if res.bucket is not None:  # already settled by a batch debit: these are extra payments
                self._mark_duplicates(p, ts, res.txns[0])
                continue
            exact = [t for t in ts if t.amount == p.amount]
            primary = exact[0] if exact else min(ts, key=lambda t: (abs((t.txn_date - p.payout_date).days), t.source_row))
            where = f"bank row {primary.source_row} ({day(primary.txn_date)}, {_lag(p, primary)})"
            if exact:
                self._claim(p, [primary], PayoutBucket.MATCHED, "R1 Payout ID + exact amount", "High",
                            f"Payout ID {p.payout_id} found in {where}; amount {money(p.amount)} matches exactly.")
            else:
                diff = primary.amount - p.amount
                self._claim(p, [primary], PayoutBucket.AMOUNT_MISMATCH, "R2 Payout ID, amount differs", "High",
                            f"Payout ID {p.payout_id} found in {where}, but the bank debited {money(primary.amount)} "
                            f"against a payout of {money(p.amount)} ({'over' if diff > 0 else 'under'}paid by "
                            f"{money(abs(diff))}).")
            extras = [t for t in ts if t is not primary]
            if extras:
                self._mark_duplicates(p, extras, primary)

    def _mark_duplicates(self, p: Payout, extras: list[BankTxn], primary: BankTxn) -> None:
        res = self.p_res[p.payout_id]
        rows = ", ".join(str(t.source_row) for t in extras)
        extra_total = sum((t.amount for t in extras), ZERO)
        res.reason += (f" ALSO debited again on bank row(s) {rows} ({money(extra_total)}): "
                       f"the employee appears to have been paid more than once.")
        if res.bucket in (PayoutBucket.MATCHED, PayoutBucket.MATCHED_BATCH):
            res.bucket = PayoutBucket.PAID_TWICE
        res.txns.extend(extras)
        for t in extras:
            self.claimed_rows.add(t.source_row)
            tr = self.t_res[t.source_row]
            tr.payout_ids.append(p.payout_id)
            tr.bucket = BankBucket.DUPLICATE_PAYMENT
            tr.rule = "R1 Payout ID (repeat)"
            tr.reason = (f"Mentions Payout ID {p.payout_id}, which was already paid on bank row {primary.source_row}. "
                         f"Possible duplicate payment of {money(t.amount)}.")

    def _match_by_attributes(self) -> None:
        def enough_evidence(p: Payout, t: BankTxn) -> int | None:
            required = STRONG_EVIDENCE if p.status in NO_DEBIT_STATUSES else SOME_EVIDENCE
            score = self._evidence(p, t)[0]
            return score if score >= required else None

        def strong_evidence_amount_differs(p: Payout, t: BankTxn) -> int | None:
            score = self._evidence(p, t)[0]
            return score if t.amount != p.amount and score >= STRONG_EVIDENCE else None

        for p, t, score in self._run_rule(_any_payout, self._same_amount, enough_evidence):
            self._claim(p, [t], PayoutBucket.MATCHED, "R4 Amount + date + account/name",
                        "High" if score > STRONG_EVIDENCE else "Medium",
                        f"No Payout ID in the bank narration. Bank row {t.source_row} has the same amount "
                        f"{money(p.amount)}, is {_lag(p, t)}, and {' and '.join(self._evidence(p, t)[1])}.")

        for p, t, _ in self._run_rule(_expects_debit, self._same_amount, _no_evidence_needed):
            self._claim(p, [t], PayoutBucket.LOW_CONFIDENCE, "R5 Amount + date only", "Low",
                        f"Bank row {t.source_row} is the only debit of {money(p.amount)} within the date window "
                        f"({_lag(p, t)}), and this is the only payout it fits. The narration "
                        f"('{t.narration}') has no Payout ID, account digits or recognisable name: please confirm.")

        for p, t, _ in self._run_rule(_expects_debit, self._same_identity, strong_evidence_amount_differs):
            diff = t.amount - p.amount
            self._claim(p, [t], PayoutBucket.AMOUNT_MISMATCH, "R6 Account/name + date, amount differs", "Medium",
                        f"Likely the same payment: bank row {t.source_row} is {_lag(p, t)} and "
                        f"{' and '.join(self._evidence(p, t)[1])}. But the bank debited {money(t.amount)} against a "
                        f"payout of {money(p.amount)} ({'over' if diff > 0 else 'under'}paid by {money(abs(diff))}).")

    def _run_rule(self, eligible: Callable[[Payout], bool], candidates: Callable[[DebitIndex, Payout], Iterable[BankTxn]],
                  score: Callable[[Payout, BankTxn], int | None]) -> list[tuple[Payout, BankTxn, int]]:
        """Score each open payout against its candidate debits; keep only pairs that are unambiguous both ways.

        `candidates` comes from an index (by amount, or by account digits / name word), so a payout is only
        compared with a handful of rows, never with the whole statement."""
        index = DebitIndex.build(self._open_debits(), self.clues)
        edges: dict[str, dict[int, int]] = {}
        for p in self.payouts:
            if self._open_payout(p) and eligible(p):
                edges[p.payout_id] = {t.source_row: s for t in candidates(index, p)
                                      if self._in_window(p, t) and (s := score(p, t)) is not None}
        return [(self.p_res[pid].payout, self.t_res[row].txn, edges[pid][row]) for pid, row in self._resolve(edges)]

    @staticmethod
    def _same_amount(index: DebitIndex, p: Payout) -> Iterable[BankTxn]:
        return index.by_amount.get(p.amount, ())

    @staticmethod
    def _same_identity(index: DebitIndex, p: Payout) -> Iterable[BankTxn]:
        found: dict[int, BankTxn] = {}
        keys = [index.by_account.get(p.account_last4, ())] if p.account_last4 else []
        keys += [index.by_word.get(w, ()) for w in name_tokens(p.employee_name) if len(w) >= 3]
        for group in keys:
            for t in group:
                found.setdefault(t.source_row, t)
        return found.values()

    def _open_debits(self) -> list[BankTxn]:
        """Unclaimed debits that could be a payout (bank charges and known non-payout rows are excluded)."""
        return [t for t in self.txns if t.direction is Direction.DEBIT and t.source_row not in self.claimed_rows
                and self.t_res[t.source_row].category in MATCHABLE_CATEGORIES]

    def _match_batches_by_sum(self) -> None:
        by_day: dict[date, list[Payout]] = defaultdict(list)
        for p in filter(self._open_payout, self.payouts):
            if p.status in (PayoutStatus.PAID, PayoutStatus.PENDING) and not self.ambig_p.get(p.payout_id):
                by_day[p.payout_date].append(p)
        days = {d: ps for d, ps in by_day.items() if len(ps) >= 2}
        if not days:
            return
        day_totals = {d: sum((p.amount for p in ps), ZERO) for d, ps in days.items()}
        hits: dict[int, list[date]] = defaultdict(list)
        for t in self._open_debits():
            for d, ps in days.items():
                if day_totals[d] == t.amount and self._in_window(ps[0], t):
                    hits[t.source_row].append(d)
        day_hits: dict[date, list[int]] = defaultdict(list)
        for row, ds in hits.items():
            for d in ds:
                day_hits[d].append(row)
        for row, ds in hits.items():
            if len(ds) == 1 and len(day_hits[ds[0]]) == 1:
                t, ps = self.t_res[row].txn, days[ds[0]]
                ids = ", ".join(p.payout_id for p in ps)
                for p in ps:
                    self._claim(p, [t], PayoutBucket.LOW_CONFIDENCE, "R7 Combined debit = day's payouts", "Low",
                                f"Bank row {row} ({money(t.amount)} on {day(t.txn_date)}) equals the total of the "
                                f"{len(ps)} payouts dated {day(ds[0])} ({ids}). No Payout IDs in the narration: "
                                f"confirm this was a bulk transfer.")

    def _link_returns(self) -> None:
        owner: dict[int, str] = {}
        for r in self.p_res.values():
            for t in r.txns:
                owner.setdefault(t.source_row, r.payout.payout_id)
        returned: set[str] = set()
        credits = [t for t in self.txns if self.t_res[t.source_row].category is TxnCategory.REVERSAL]

        for c in credits:
            tr = self.t_res[c.source_row]
            link: tuple[str, str] | None = None
            mentioned = [p for p in self.mentions[c.source_row] if self.p_res[p.payout_id].txns]
            if len(mentioned) == 1:
                link = (mentioned[0].payout_id, f"mentions Payout ID {mentioned[0].payout_id}")
            elif c.reference and len(compact(c.reference)) >= 4:
                same_ref = [row for row in owner if compact(self.t_res[row].txn.reference) == compact(c.reference)
                            and self.t_res[row].txn.txn_date <= c.txn_date]
                if len(same_ref) == 1:
                    link = (owner[same_ref[0]], f"has the same reference '{c.reference}' as bank row {same_ref[0]}")
            if link is None:
                scored = []
                for row, pid in owner.items():
                    d = self.t_res[row].txn
                    gap = (c.txn_date - d.txn_date).days
                    if d.amount == c.amount and 0 <= gap <= self.cfg.return_window_days and pid not in returned:
                        score, _ = self._evidence(self.p_res[pid].payout, c)
                        scored.append((score, row, pid))
                if scored:
                    top = max(s for s, _, _ in scored)
                    best = [(row, pid) for s, row, pid in scored if s == top]
                    if len(best) == 1:
                        row, pid = best[0]
                        link = (pid, f"returns the same amount {money(c.amount)} as bank row {row}"
                                     + (" and names the same employee/account" if top else ""))
            if link is None or link[0] in returned:
                tr.bucket = BankBucket.RETURN_UNLINKED
                ids = ", ".join(p.payout_id for p in self.mentions[c.source_row])
                tr.reason = (f"Return/reversal credit of {money(c.amount)} ({tr.category_reason}) that could not be "
                             f"tied to a paid payout" + (f" (mentions {ids}, but no debit for it was found)" if ids else "")
                             + ". Find out which payment bounced.")
                continue

            pid, how = link
            returned.add(pid)
            res = self.p_res[pid]
            res.return_txn = c
            tr.bucket, tr.payout_ids, tr.rule = BankBucket.RETURN_LINKED, [pid], "RT Return linked"
            tr.reason = f"Return of payout {pid}: this credit {how}."
            partial = "" if c.amount == res.bank_amount else f" Only {money(c.amount)} of {money(res.bank_amount)} came back."
            if res.payout.status in (PayoutStatus.FAILED, PayoutStatus.REVERSED):
                res.bucket = PayoutBucket.RETURN_CONSISTENT
                res.reason += (f" The bank returned it on {day(c.txn_date)} (row {c.source_row}), which agrees with "
                               f"the register status {res.payout.raw_status}.{partial}")
            else:
                res.bucket = PayoutBucket.RETURNED
                res.reason += (f" Then RETURNED by the bank on {day(c.txn_date)} (row {c.source_row}: "
                               f"'{c.narration}'). The employee has not received this money.{partial}")

    def _finalize(self) -> None:
        end = self.period[1] if self.period else None
        for r in self.p_res.values():
            p = r.payout
            if r.bucket is None:
                self._unmatched_payout(r, end)
            elif r.bucket not in (PayoutBucket.RETURN_CONSISTENT,) and r.txns:
                if p.status in NO_DEBIT_STATUSES:
                    r.reason = (f"Register says {p.raw_status}, but the bank shows a debit. "
                                f"Check whether the employee was paid. ({r.reason})")
                    r.bucket = PayoutBucket.STATUS_CONFLICT
                elif p.status is PayoutStatus.REVERSED and r.return_txn is None:
                    r.reason = f"Register says {p.raw_status}, but no return credit is in this statement. ({r.reason})"
                    r.bucket = PayoutBucket.STATUS_CONFLICT
                elif p.status is PayoutStatus.PENDING and r.bucket in (PayoutBucket.MATCHED, PayoutBucket.MATCHED_BATCH):
                    r.reason += f" Register still says {p.raw_status}: update the status in Chronon."
                    r.bucket = PayoutBucket.STATUS_CONFLICT

        settled_ok = {PayoutBucket.MATCHED, PayoutBucket.MATCHED_BATCH, PayoutBucket.RETURN_CONSISTENT}
        for r in self.p_res.values():
            for t in r.txns:
                tr = self.t_res[t.source_row]
                if tr.bucket is None:
                    tr.bucket = BankBucket.MATCHED if r.bucket in settled_ok else BankBucket.MATCHED_REVIEW
                    tr.reason = f"Payout {', '.join(tr.payout_ids)}: {r.bucket.value}."

        for tr in self.t_res.values():
            if tr.bucket is not None:
                continue
            t = tr.txn
            row = t.source_row
            if t.direction is Direction.CREDIT:
                tr.bucket = BankBucket.OTHER_CREDIT
                tr.reason = f"Credit not related to payouts ({tr.category_reason})."
            elif open_pids := sorted(pid for pid in self.ambig_t.get(row, ())
                                     if self.p_res[pid].bucket is PayoutBucket.AMBIGUOUS):
                tr.bucket = BankBucket.AMBIGUOUS
                tr.payout_ids = open_pids
                tr.reason = (f"Fits more than one payout equally well ({', '.join(tr.payout_ids)}). "
                             f"Choose the right one.")
            elif tr.category is TxnCategory.BANK_CHARGE:
                tr.bucket = BankBucket.BANK_CHARGE
                tr.reason = f"Bank charge ({tr.category_reason})."
            elif tr.category is TxnCategory.PAYOUT_CANDIDATE:
                tr.bucket = BankBucket.UNEXPLAINED
                tr.reason = (f"Looks like a transfer ({tr.category_reason}) but no payout in the register matches "
                             f"{money(t.amount)} around {day(t.txn_date)}. Find out who was paid and why.")
            else:
                tr.bucket = BankBucket.NON_PAYOUT
                tr.reason = f"Not a payout ({tr.category_reason}) and matches no payout."

    def _unmatched_payout(self, r: PayoutResult, end: date | None) -> None:
        p = r.payout
        open_rows = sorted(row for row in self.ambig_p.get(p.payout_id, ()) if row not in self.claimed_rows)
        if open_rows:
            r.bucket, r.rule, r.confidence = PayoutBucket.AMBIGUOUS, "Ambiguous", "—"
            r.ambiguous_candidates = [self.t_res[row].txn for row in open_rows]
            rows = ", ".join(str(t.source_row) for t in r.ambiguous_candidates)
            r.reason = (f"More than one bank debit fits this payout equally well (rows {rows}), or that debit fits "
                        f"several payouts. The tool will not guess: pick the right row.")
            return
        if p.status in NO_DEBIT_STATUSES:
            r.bucket = PayoutBucket.NO_DEBIT_EXPECTED
            r.reason = f"Register says {p.raw_status} and no bank debit was found, as expected."
        elif p.status is PayoutStatus.PENDING:
            r.bucket = PayoutBucket.NOT_YET_DEBITED
            r.reason = f"Register says {p.raw_status} and the bank has no debit yet. Re-check next statement."
        elif p.status is PayoutStatus.REVERSED:
            r.bucket = PayoutBucket.NO_DEBIT_EXPECTED
            r.reason = f"Register says {p.raw_status}; neither the debit nor the return is in this statement."
        else:
            r.bucket = PayoutBucket.MISSING_IN_BANK
            r.reason = f"Register says {p.raw_status} on {day(p.payout_date)}, but no bank debit matches it."
            if end and p.payout_date > end:
                r.reason += f" The payout date is after the statement ends ({day(end)}): expect it on the next statement."
            elif end and (end - p.payout_date).days < self.cfg.in_transit_days:
                r.reason += f" It was paid within {self.cfg.in_transit_days} days of the statement end ({day(end)}), so it may still be in transit."
            elif self.period and p.payout_date < self.period[0]:
                r.reason += f" The payout date is before the statement starts ({day(self.period[0])}): check the previous statement."
        r.rule, r.confidence = "No match", "—"
