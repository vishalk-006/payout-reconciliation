from datetime import date
from decimal import Decimal

import pytest

from app.layout.models import LayoutError
from app.layout.providers import ProviderNotConfigured
from app.models import Direction, PayoutStatus
from app.parse_bank import parse_bank_statement
from app.parse_payouts import parse_payout_register
from tests.conftest import BANK_HEADER, REGISTER_HEADER, ExplodingDetector, FakeDetector, answer


def bank_rows(*txns, opening="10,000.00", preamble=True, header=BANK_HEADER):
    rows = [["XYZ BANK"], ["Account 123"], []] if preamble else []
    rows.append(header)
    rows.append([None, None, "Opening Balance", None, None, None, opening])
    rows.extend(txns)
    return rows


# --------------------------------------------------------------------------- bank statement

def test_bank_header_found_below_preamble_and_noise_classified(xlsx):
    path = xlsx(bank_rows(
        ["03-02-2026", "03-02-2026", "NEFT/A", "R1", "1,000.00", None, "9,000.00"],
        BANK_HEADER,  # page-break header
        [],
        ["04-02-2026", "04-02-2026", "IMPS/B", "R2", None, "500", "9,500.00"],
        [None, None, "Closing Balance", None, None, None, "9,500.00"],
        ["Page 1 of 1"],
    ))
    res = parse_bank_statement(path)
    assert res.header_row == 4
    assert [t.amount for t in res.rows] == [Decimal("1000.00"), Decimal("500.00")]
    assert [t.direction for t in res.rows] == [Direction.DEBIT, Direction.CREDIT]
    reasons = [n.reason for n in res.noise]
    assert any("Repeated header" in r for r in reasons)
    assert any("Opening balance" in r for r in reasons) and any("Closing balance" in r for r in reasons)
    assert any("Text-only" in r for r in reasons)
    assert res.accounting.balanced and not res.rejected
    assert res.balance_check.status == "VERIFIED"


def test_bank_bad_rows_are_rejected_not_dropped(xlsx):
    path = xlsx(bank_rows(
        ["03-02-2026", None, "NEFT/A", "R1", "12O0", None, "8,800.00"],
        ["31/02/2026", None, "NEFT/B", "R2", "200", None, "8,600.00"],
        ["04-02-2026", None, "NEFT/C", "R3", "100", "100", "8,600.00"],
        ["05-02-2026", None, "NEFT/D", "R4", None, None, "8,600.00"],
    ))
    res = parse_bank_statement(path)
    codes = sorted(r.reason.split(":")[0] for r in res.rejected)
    assert codes == ["BOTH_DEBIT_AND_CREDIT", "NO_AMOUNT", "UNPARSEABLE_AMOUNT", "UNPARSEABLE_DATE"]
    assert res.accounting.balanced
    # The invalid-date row's amount is still known so totals can account for it.
    bad_date = next(r for r in res.rejected if r.reason.startswith("UNPARSEABLE_DATE"))
    assert bad_date.amount == Decimal("200.00") and bad_date.direction is Direction.DEBIT


def test_balance_chain_detects_missing_row(xlsx):
    path = xlsx(bank_rows(
        ["03-02-2026", None, "NEFT/A", "R1", "1,000.00", None, "9,000.00"],
        # a 500 debit row is missing here
        ["04-02-2026", None, "NEFT/C", "R3", "200", None, "8,300.00"],
    ))
    res = parse_bank_statement(path)
    assert res.balance_check.status == "BROKEN"
    brk = next(i for i in res.issues if i.code == "BALANCE_BREAK")
    assert brk.source_row == 7 and "8,800.00" in brk.message


def test_balance_chain_detects_wrong_sign_and_closing_mismatch(xlsx):
    path = xlsx(bank_rows(
        ["03-02-2026", None, "NEFT/A", "R1", None, "1,000.00", "9,000.00"],  # really a debit, typed as credit
        [None, None, "Closing Balance", None, None, None, "9,000.00"],
    ))
    res = parse_bank_statement(path)
    assert res.balance_check.status == "BROKEN"


def test_newest_first_statement_verifies(xlsx):
    path = xlsx([BANK_HEADER,
                 ["05-02-2026", None, "NEFT/B", "R2", "500", None, "8,500.00"],
                 ["03-02-2026", None, "NEFT/A", "R1", "1,500", None, "9,000.00"]])
    res = parse_bank_statement(path)
    assert res.balance_check.status == "VERIFIED"


def test_multiline_narration_is_merged(xlsx):
    path = xlsx(bank_rows(
        ["03-02-2026", None, "NEFT/CHRONON/PO-", "R1", "1,000.00", None, "9,000.00"],
        [None, None, "2026-0101/RAHUL"],
    ))
    res = parse_bank_statement(path)
    assert res.rows[0].narration == "NEFT/CHRONON/PO- 2026-0101/RAHUL"
    assert any("continued from row 6" in n.reason for n in res.noise)


def test_vendor_named_total_is_not_a_totals_line(xlsx):
    path = xlsx(bank_rows(["03-02-2026", None, "TOTAL SOLUTIONS PVT LTD", "R1", "1,000.00", None, "9,000.00"]))
    assert len(parse_bank_statement(path).rows) == 1


def test_footer_totals_are_checked(xlsx):
    path = xlsx(bank_rows(
        ["03-02-2026", None, "NEFT/A", "R1", "1,000.00", None, "9,000.00"],
        [None, None, "Total", None, "1,100.00", "0.00", None],
    ))
    res = parse_bank_statement(path)
    assert any(i.code == "FOOTER_TOTAL_MISMATCH" for i in res.issues)


def test_ambiguous_dates_are_not_guessed(xlsx, store):
    """The LLM cannot tell dd/mm from mm/dd either (UNKNOWN): the tool refuses instead of guessing."""
    path = xlsx([["Date", "Details", "Withdrawal"], ["03-02-2026", "A", "1"], ["03-02-2026", "B", "1"]])
    llm = FakeDetector(answer("Sheet", 1, {"txn_date": "Date", "narration": "Details", "amount": "Withdrawal"},
                              date_order="UNKNOWN"))
    with pytest.raises(LayoutError, match="day-first or month-first"):
        parse_bank_statement(path, store=store, detector=llm)
    assert len(llm.requests) == 2 and "day-first" in llm.requests[1].feedback[0]


# --------------------------------------------------------------------------- new bank formats (LLM layouts)

def test_different_bank_layouts_give_same_canonical_rows(xlsx, store):
    """Different column names, order, signed Amount or Amount + Dr/Cr, month-name dates: the LLM only
    says where things are; the same deterministic parser reads them all identically."""
    a = xlsx(bank_rows(["03-02-2026", None, "NEFT/A", "R1", "1,000.00", None, "9,000.00"],
                       ["04-02-2026", None, "NEFT/B", "R2", None, "250.00", "9,250.00"]))
    b = xlsx([["Statement"], ["Tran Date", "Particulars", "Chq./Ref.No.", "Amount", "Closing Balance"],
              [None, "Opening Balance", None, None, "10,000.00"],
              ["03-Feb-2026", "NEFT/A", "R1", "-1,000.00", "9,000.00"],
              ["04-Feb-2026", "NEFT/B", "R2", "250.00", "9,250.00"]])
    c = xlsx([["Date", "Description", "Amount", "Dr/Cr", "Balance"],
              ["03/02/2026", "NEFT/A", "1000", "DR", "9000"],
              ["04/02/2026", "NEFT/B", "250", "CR", "9250"]])
    llm_b = FakeDetector(answer("Sheet", 2, {"txn_date": "Tran Date", "narration": "Particulars",
                                             "reference": "Chq./Ref.No.", "amount": "Amount",
                                             "balance": "Closing Balance"}, negative="DEBIT"))
    llm_c = FakeDetector(answer("Sheet", 1, {"txn_date": "Date", "narration": "Description", "amount": "Amount",
                                             "dr_cr": "Dr/Cr", "balance": "Balance"}))

    def key(res):
        return [(t.txn_date, t.narration, t.direction, t.amount, t.balance) for t in res.rows]
    ra = parse_bank_statement(a, store=store, detector=ExplodingDetector())  # known (seeded) layout
    rb = parse_bank_statement(b, store=store, detector=llm_b)
    rc = parse_bank_statement(c, store=store, detector=llm_c)
    assert key(ra) == key(rb) == key(rc)
    assert (ra.amount_style, rb.amount_style, rc.amount_style) == ("split", "signed", "indicator")
    assert ra.layout.source == "seeded" and rb.layout.verified and rc.layout.llm_called


def test_unknown_layout_without_llm_configured_says_so(xlsx, store):
    path = xlsx([["When", "What", "How much"], ["03-02-2026", "x", "1"]])
    with pytest.raises(ProviderNotConfigured, match="GEMINI_API_KEY"):
        parse_bank_statement(path, store=store)  # default provider gemini, no key in the environment
    with pytest.raises(ProviderNotConfigured, match="Unknown LLM provider"):
        parse_bank_statement(path, store=store, provider="nope")


def test_learned_layout_is_reused_without_calling_the_llm(xlsx, store):
    rows = [["Posted On", "Memo", "Money Out", "Money In", "Bal"],
            ["02-03-2026", "NEFT/A", "10", None, "90"], ["02-04-2026", "NEFT/B", None, "5", "95"]]
    llm = FakeDetector(answer("Sheet", 1, {"txn_date": "Posted On", "narration": "Memo", "debit": "Money Out",
                                           "credit": "Money In", "balance": "Bal"}, date_order="MDY"))
    first = parse_bank_statement(xlsx(rows), store=store, detector=llm)
    assert first.layout.llm_called and first.rows[0].txn_date == date(2026, 2, 3)  # US month-first dates
    again = parse_bank_statement(xlsx(rows + [["02-05-2026", "NEFT/C", "1", None, "94"]]), store=store,
                                 detector=ExplodingDetector())
    assert not again.layout.llm_called and again.layout.source == "fake:test-model" and len(again.rows) == 3
    assert len(llm.requests) == 1


# --------------------------------------------------------------------------- payout register

def test_register_parsing_and_validation(xlsx):
    path = xlsx([REGISTER_HEADER,
                 ["PO-1", "E1", "Rahul Kumar", "XXXXXX4521", "HDFC0001234", "1,000.00", "03-02-2026", "CL-1, CL-2", "Success"],
                 ["PO-2", "E2", "Priya", "XXXX", "BADIFSC", 500, "03-02-2026", "CL-3", "Processing"],
                 ["PO-3", "E3", "Amit", "XXXXXX1111", "SBIN0001111", "12O0", "03-02-2026", "CL-4", "PAID"],
                 ["PO-4", "E4", "Neha", "XXXXXX2222", "SBIN0001111", 10, "03-02-2026", "CL-5", "On hold"],
                 ["PO-1", "E1", "Rahul Kumar", "XXXXXX4521", "HDFC0001234", 1000, "03-02-2026", "CL-1", "PAID"],
                 [None, None, None, None, None, "2,510.00", None, None, None],
                 ["TOTAL", None, None, None, None, "2,500.00", None, None, None]])
    res = parse_payout_register(path)
    assert [p.payout_id for p in res.rows] == ["PO-1", "PO-2"]
    p1, p2 = res.rows
    assert p1.status is PayoutStatus.PAID and p1.claim_ids == ["CL-1", "CL-2"] and p1.account_last4 == "4521"
    assert p2.status is PayoutStatus.PENDING and p2.account_last4 is None and len(p2.warnings) == 2
    reasons = " ".join(r.reason for r in res.rejected)
    assert "UNPARSEABLE_AMOUNT" in reasons and "UNKNOWN_STATUS" in reasons and "DUPLICATE_PAYOUT_ID" in reasons
    assert "MISSING_PAYOUT_ID" in reasons  # the row with only an amount
    assert res.accounting.balanced


def test_register_total_row_is_a_control(xlsx):
    path = xlsx([REGISTER_HEADER,
                 ["PO-1", "E1", "A", "X1234", "", 100, "03-02-2026", "", "PAID"],
                 [None, None, "Grand Total", None, None, 150, None, None, None]])
    res = parse_payout_register(path)
    assert res.control_total == Decimal("150.00")
    assert any(i.code == "CONTROL_TOTAL_MISMATCH" for i in res.issues)


def test_sample_data_end_to_end(tmp_path):
    import make_sample_data as m
    m.make_payout_register(tmp_path / "p.xlsx")
    m.make_bank_statement(tmp_path / "b.xlsx")
    bank = parse_bank_statement(tmp_path / "b.xlsx")
    assert bank.balance_check.status == "VERIFIED"
    assert not bank.rejected and bank.accounting.balanced
    reg = parse_payout_register(tmp_path / "p.xlsx")
    assert len(reg.rows) == 16 and len(reg.rejected) == 2


def test_statement_summary_footer_is_read_not_rejected(xlsx, store):
    from tests.conftest import FakeDetector, answer
    path = xlsx([["Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"],
                 ["03/02/26", "NEFT/A", 1000, None, 9000], ["04/02/26", "NEFT/B", None, 500, 9500],
                 ["STATEMENT SUMMARY :-"],
                 ["Opening Balance", "Dr Count", "Cr Count", "Debits", "Credits", "Closing Bal"],
                 [10000, 1, 1, 1000, 500, 9500],
                 ["Generated On: 16-Mar-2026  This is a computer generated statement."]])
    llm = FakeDetector(answer("Sheet", 1, {"txn_date": "Date", "narration": "Narration", "debit": "Withdrawal Amt.",
                                           "credit": "Deposit Amt.", "balance": "Closing Balance"}))
    res = parse_bank_statement(path, store=store, detector=llm)
    assert not res.rejected and len(res.rows) == 2
    assert res.balance_check.opening == Decimal("10000.00") and res.balance_check.closing == Decimal("9500.00")
    assert res.balance_check.status == "VERIFIED"
    assert not any(i.code == "FOOTER_TOTAL_MISMATCH" for i in res.issues)  # footer totals agree


def test_letter_o_in_amount_is_explained(xlsx):
    path = xlsx(bank_rows(["03-02-2026", None, "NEFT/A", "R1", "1,2O0.00", None, "8,800.00"]))
    assert "letter O" in parse_bank_statement(path).rejected[0].reason
