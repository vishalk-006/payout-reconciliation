"""Bank row classification and narration clue extraction. Deterministic, keyword-driven
(rules in app/rules/classification.yaml) so finance can extend it without a code change."""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

import yaml

from .cleaners import normalize_text
from .models import BankTxn, Direction, TxnCategory

RULES_FILE = Path(__file__).parent / "rules" / "classification.yaml"
MAX_NGRAM = 4
NAME_TITLES = {"MR", "MRS", "MS", "MISS", "DR", "SHRI", "SMT", "KUM"}
FUZZY_TOKEN_RATIO = 0.85


@lru_cache
def keyword_rules() -> dict[str, tuple[str, ...]]:
    data = yaml.safe_load(RULES_FILE.read_text())
    return {k: tuple(normalize_text(w) for w in v) for k, v in data.items()}


@dataclass(frozen=True)
class Clues:
    norm: str
    words: tuple[str, ...]
    joined: frozenset[str]  # words, plus up to MAX_NGRAM adjacent words glued: 'PO 2026 01' -> 'PO202601'
    digit_suffixes: frozenset[str]  # last 4 digits of every digit run of length >= 4


def joined_words(text: str) -> frozenset[str]:
    words = normalize_text(text).split()
    return frozenset("".join(words[i:i + n]) for n in range(1, MAX_NGRAM + 1) for i in range(len(words) - n + 1))


def extract_clues(txn: BankTxn) -> Clues:
    norm = normalize_text(f"{txn.narration} {txn.reference}")
    words = tuple(norm.split())
    joined = joined_words(norm)
    # Account digits are often glued to letters ('XX7788', 'A/C50100123'), so look inside words too.
    digit_runs = re.findall(r"\d{4,}", norm)
    return Clues(norm, words, joined, frozenset(r[-4:] for r in digit_runs))


def has_phrase(clues: Clues, phrases: tuple[str, ...]) -> str | None:
    padded = f" {clues.norm} "
    return next((p for p in phrases if f" {p} " in padded), None)


def classify(txn: BankTxn, clues: Clues, mentions_payout: bool) -> tuple[TxnCategory, str]:
    """Returns (category, reason)."""
    r = keyword_rules()
    if txn.direction is Direction.CREDIT:
        if kw := has_phrase(clues, r["reversal_credit"]):
            return TxnCategory.REVERSAL, f"credit with return keyword '{kw}'"
        if mentions_payout:
            return TxnCategory.REVERSAL, "credit that mentions a Payout ID"
        return TxnCategory.OTHER_CREDIT, "credit without return keywords"
    if mentions_payout:
        return TxnCategory.PAYOUT_CANDIDATE, "narration mentions a Payout ID"
    if kw := has_phrase(clues, r["bank_charge"]):
        return TxnCategory.BANK_CHARGE, f"bank charge keyword '{kw}'"
    if kw := has_phrase(clues, r["non_payout_debit"]):
        return TxnCategory.NON_PAYOUT, f"non-payout keyword '{kw}'"
    if kw := has_phrase(clues, r["payout_channel"]):
        return TxnCategory.PAYOUT_CANDIDATE, f"transfer keyword '{kw}'"
    return TxnCategory.OTHER_DEBIT, "no keyword matched"


NAME_NONE, NAME_PARTIAL, NAME_FULL = 0, 1, 2


def name_tokens(name: str) -> list[str]:
    return [t for t in normalize_text(name).split() if t not in NAME_TITLES]


def _token_hit(token: str, clues: Clues) -> bool:
    if token in clues.words:
        return True
    if len(token) >= 4:
        return any(len(w) >= 4 and SequenceMatcher(None, token, w).ratio() >= FUZZY_TOKEN_RATIO for w in clues.words)
    return False


def name_evidence(name: str, clues: Clues) -> int:
    """FULL: every part of the name appears (typos tolerated, or written without spaces).
    PARTIAL: first name appears and the other parts appear as initials, or 2+ parts appear."""
    tokens = name_tokens(name)
    if not tokens:
        return NAME_NONE
    if len(tokens) > 1 and "".join(tokens) in clues.joined:
        return NAME_FULL
    full = [_token_hit(t, clues) for t in tokens]
    if all(full):
        return NAME_FULL if len(tokens) > 1 or len(tokens[0]) >= 4 else NAME_PARTIAL
    initials = [not f and t[0] in clues.words for t, f in zip(tokens, full, strict=True)]
    if full[0] and all(f or i for f, i in zip(full, initials, strict=True)):
        return NAME_PARTIAL
    if sum(full) >= 2:
        return NAME_PARTIAL
    return NAME_NONE


def account_evidence(last4: str | None, clues: Clues) -> bool:
    return bool(last4) and last4 in clues.digit_suffixes
