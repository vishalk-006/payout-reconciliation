from datetime import date, timedelta

from app.matching import BankBucket, PayoutBucket, reconcile
from app.models import Direction, PayoutStatus
from tests.conftest import payout, txn

D = date(2026, 2, 3)


def outcome(res, pid):
    return next(r for r in res.payouts if r.payout.payout_id == pid)


def bank(res, row):
    return next(r for r in res.txns if r.txn.source_row == row)


def test_r1_payout_id_exact_amount():
    res = reconcile([payout("PO-1001")], [txn("NEFT/CHRONON/PO-1001/RAHUL", row=10)])
    r = outcome(res, "PO-1001")
    assert r.bucket is PayoutBucket.MATCHED and r.rule.startswith("R1") and r.confidence == "High"
    assert "PO-1001" in r.reason and "matches exactly" in r.reason
    assert bank(res, 10).bucket is BankBucket.MATCHED


def test_payout_id_found_despite_different_separators():
    res = reconcile([payout("PO-2026-0114")], [txn("NEFT-CHRONON-PO20260114-FARHAN")])
    assert outcome(res, "PO-2026-0114").bucket is PayoutBucket.MATCHED


def test_short_ids_are_not_matched_by_coincidence():
    res = reconcile([payout("101", name="Zed Q", account="")], [txn("CHQ 101 DEPOSIT REF", amount="999")])
    assert outcome(res, "101").bucket is PayoutBucket.MISSING_IN_BANK


def test_r2_id_found_but_amount_differs():
    res = reconcile([payout("PO-1001", amount="1000")], [txn("NEFT PO-1001", amount="1050")])
    r = outcome(res, "PO-1001")
    assert r.bucket is PayoutBucket.AMOUNT_MISMATCH and "overpaid by ₹50.00" in r.reason
    assert bank(res, 10).bucket is BankBucket.MATCHED_REVIEW


def test_r3_batch_debit_with_several_ids():
    ps = [payout("PO-1001", amount="300"), payout("PO-1002", amount="700", name="Priya Shah", row=3)]
    res = reconcile(ps, [txn("NEFT BULK PO-1001,PO-1002", amount="1000")])
    assert {outcome(res, p).bucket for p in ("PO-1001", "PO-1002")} == {PayoutBucket.MATCHED_BATCH}


def test_duplicate_payment_detected():
    res = reconcile([payout("PO-1001")], [txn("IMPS PO-1001", row=10), txn("IMPS PO-1001", row=11, on=D + timedelta(1))])
    assert outcome(res, "PO-1001").bucket is PayoutBucket.PAID_TWICE
    assert bank(res, 11).bucket is BankBucket.DUPLICATE_PAYMENT


def test_r4_account_and_name_without_id():
    res = reconcile([payout("PO-1001", name="Priya Shah", account="XXXXXX7788")],
                    [txn("IMPS/P2A/PRIYA S/XXXXXX7788", on=D + timedelta(1))])
    r = outcome(res, "PO-1001")
    assert r.bucket is PayoutBucket.MATCHED and r.rule.startswith("R4") and r.confidence == "High"
    assert "7788" in r.reason and "1 day after" in r.reason


def test_name_typo_tolerated():
    res = reconcile([payout("PO-1001", name="Rahul Kumar", account="")], [txn("NEFT RAHUL KUMR")])
    assert outcome(res, "PO-1001").bucket is PayoutBucket.MATCHED


def test_name_picks_the_right_one_of_two_same_amount_payouts():
    ps = [payout("PO-1001", name="Rahul Kumar", account=""), payout("PO-1002", name="Amit Verma", account="", row=3)]
    ts = [txn("NEFT AMIT VERMA", row=10), txn("NEFT RAHUL KUMAR", row=11)]
    res = reconcile(ps, ts)
    assert outcome(res, "PO-1001").txns[0].source_row == 11
    assert outcome(res, "PO-1002").txns[0].source_row == 10


def test_r5_amount_and_date_only_needs_confirmation():
    res = reconcile([payout("PO-1001", account="")], [txn("IMPS TRF 98231")])
    r = outcome(res, "PO-1001")
    assert r.bucket is PayoutBucket.LOW_CONFIDENCE and r.confidence == "Low" and r.needs_review


def test_same_amount_same_day_is_ambiguous_not_guessed():
    ps = [payout("PO-1001", name="Divya P", account=""), payout("PO-1002", name="Suresh B", account="", row=3)]
    res = reconcile(ps, [txn("IMPS/P2A/REIMB", row=10), txn("IMPS/P2A/REIMB", row=11)])
    assert {outcome(res, p).bucket for p in ("PO-1001", "PO-1002")} == {PayoutBucket.AMBIGUOUS}
    assert {bank(res, r).bucket for r in (10, 11)} == {BankBucket.AMBIGUOUS}


def test_date_window_boundaries():
    p = payout("PO-1001", account="")
    inside = reconcile([p], [txn("IMPS X", on=D + timedelta(5))])
    outside = reconcile([p], [txn("IMPS X", on=D + timedelta(6))])
    before = reconcile([p], [txn("IMPS X", on=D - timedelta(2))])
    assert outcome(inside, "PO-1001").bucket is PayoutBucket.LOW_CONFIDENCE
    assert outcome(outside, "PO-1001").bucket is PayoutBucket.MISSING_IN_BANK
    assert outcome(before, "PO-1001").bucket is PayoutBucket.MISSING_IN_BANK


def test_r6_name_match_amount_differs():
    res = reconcile([payout("PO-1001", name="Amit Verma", amount="900", account="")], [txn("NEFT/AMIT VERMA", amount="950")])
    assert outcome(res, "PO-1001").bucket is PayoutBucket.AMOUNT_MISMATCH


def test_r7_bulk_debit_equal_to_days_payouts():
    ps = [payout("PO-1001", amount="300", account="", name="A B"), payout("PO-1002", amount="700", account="", name="C D", row=3)]
    res = reconcile(ps, [txn("NEFT BULK UPLOAD", amount="1000")])
    assert {outcome(res, p).bucket for p in ("PO-1001", "PO-1002")} == {PayoutBucket.LOW_CONFIDENCE}


def test_return_linked_to_payout():
    res = reconcile([payout("PO-1001")], [
        txn("NEFT PO-1001", row=10),
        txn("NEFT RETURN INVALID A/C", direction=Direction.CREDIT, on=D + timedelta(3), row=11)])
    assert outcome(res, "PO-1001").bucket is PayoutBucket.RETURNED
    assert bank(res, 11).bucket is BankBucket.RETURN_LINKED


def test_return_on_failed_payout_is_consistent():
    res = reconcile([payout("PO-1001", status=PayoutStatus.FAILED)], [
        txn("NEFT PO-1001", row=10),
        txn("RTN PO-1001", direction=Direction.CREDIT, on=D + timedelta(2), row=11)])
    r = outcome(res, "PO-1001")
    assert r.bucket is PayoutBucket.RETURN_CONSISTENT and not r.needs_review


def test_unlinked_return_needs_review():
    res = reconcile([], [txn("NEFT RETURN XYZ", direction=Direction.CREDIT)])
    assert bank(res, 10).bucket is BankBucket.RETURN_UNLINKED


def test_failed_payout_with_debit_is_a_status_conflict():
    res = reconcile([payout("PO-1001", status=PayoutStatus.FAILED)], [txn("NEFT PO-1001")])
    assert outcome(res, "PO-1001").bucket is PayoutBucket.STATUS_CONFLICT


def test_failed_payout_not_matched_on_amount_alone():
    res = reconcile([payout("PO-1001", status=PayoutStatus.FAILED, account="")], [txn("IMPS TRF 1")])
    assert outcome(res, "PO-1001").bucket is PayoutBucket.NO_DEBIT_EXPECTED
    assert bank(res, 10).bucket is BankBucket.UNEXPLAINED


def test_pending_payout_states():
    unpaid = reconcile([payout("PO-1001", status=PayoutStatus.PENDING)], [])
    paid = reconcile([payout("PO-1001", status=PayoutStatus.PENDING)], [txn("NEFT PO-1001")])
    assert outcome(unpaid, "PO-1001").bucket is PayoutBucket.NOT_YET_DEBITED
    assert outcome(paid, "PO-1001").bucket is PayoutBucket.STATUS_CONFLICT


def test_non_payout_rows_are_classified_not_matched():
    res = reconcile([payout("PO-1001", amount="59", account="")], [
        txn("CONSOLIDATED CHARGES", amount="59", row=10),
        txn("NEFT SALARY FEB", amount="45000", row=11),
        txn("NEFT ACME PVT LTD INV 44", amount="1000", row=12),
        txn("INT.PD", amount="10", direction=Direction.CREDIT, row=13)])
    assert outcome(res, "PO-1001").bucket is PayoutBucket.MISSING_IN_BANK  # not matched to the bank charge
    assert [bank(res, r).bucket for r in (10, 11, 12, 13)] == [
        BankBucket.BANK_CHARGE, BankBucket.NON_PAYOUT, BankBucket.NON_PAYOUT, BankBucket.OTHER_CREDIT]


def test_unexplained_transfer_flagged():
    res = reconcile([], [txn("IMPS/RAMESH K/XX0099", amount="3333")])
    assert bank(res, 10).bucket is BankBucket.UNEXPLAINED and bank(res, 10).needs_review


def test_missing_payout_near_statement_end_mentions_transit():
    res = reconcile([payout("PO-1001", on=D + timedelta(9), account="", amount="5")],
                    [txn("NEFT X", on=D + timedelta(10), amount="1")])
    assert "in transit" in outcome(res, "PO-1001").reason


def test_every_item_lands_in_exactly_one_bucket_and_result_is_deterministic():
    ps = [payout(f"PO-10{i:02}", amount=str(100 + i % 3), name=f"Name{i} X", account="", row=i) for i in range(30)]
    ts = [txn(f"IMPS {i}", amount=str(100 + i % 4), on=D + timedelta(i % 3), row=100 + i) for i in range(30)]
    a, b = reconcile(ps, ts), reconcile(list(reversed(ps)), list(reversed(ts)))
    assert all(r.bucket is not None for r in a.payouts) and all(r.bucket is not None for r in a.txns)
    assert len(a.payouts) == 30 and len(a.txns) == 30
    sig = lambda res: sorted((r.payout.payout_id, r.bucket, tuple(t.source_row for t in r.txns)) for r in res.payouts)  # noqa: E731
    assert sig(a) == sig(b)  # input order does not change the outcome


def test_r6_account_digits_alone_find_amount_mismatch():
    res = reconcile([payout("PO-1001", name="Zed Q", amount="900", account="XXXXXX7788")],
                    [txn("IMPS/XXXXXX7788/TRF", amount="950")])
    assert outcome(res, "PO-1001").bucket is PayoutBucket.AMOUNT_MISMATCH


def test_matcher_does_not_mutate_its_inputs():
    t = txn("NEFT PO-1001")
    before = dict(vars(t))
    reconcile([payout("PO-1001")], [t])
    assert vars(t) == before


def test_gst_payment_is_not_a_bank_charge_but_charges_with_gst_are():
    res = reconcile([], [txn("GST PAYMENT CHALLAN", amount="45000", row=10), txn("IMPS CHGS GST", amount="5.90", row=11)])
    assert bank(res, 10).bucket is BankBucket.NON_PAYOUT and bank(res, 11).bucket is BankBucket.BANK_CHARGE
