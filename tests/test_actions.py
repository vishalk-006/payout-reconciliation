import random
from decimal import Decimal

import make_more_samples as scenarios
import make_sample_data as sample

from app.actions import Action, build_actions
from app.matching import reconcile
from app.parse_bank import parse_bank_statement
from app.parse_payouts import parse_payout_register


def actions_for(register, statement):
    payouts, bank = parse_payout_register(register), parse_bank_statement(statement)
    return build_actions(payouts, bank, reconcile(payouts.rows, bank.rows)), bank


def by_payout(actions):
    return {a.payout_id: a for a in actions if a.payout_id}


def test_every_kind_of_problem_becomes_a_plain_action(tmp_path):
    sample.make_payout_register(tmp_path / "p.xlsx")
    sample.make_bank_statement(tmp_path / "b.xlsx")
    actions, _ = actions_for(tmp_path / "p.xlsx", tmp_path / "b.xlsx")
    found = by_payout(actions)

    assert found["PO-2026-0105"].action is Action.PAY_AGAIN and found["PO-2026-0105"].amount == Decimal("3100.00")
    assert "came back" in found["PO-2026-0105"].what_happened
    assert found["PO-2026-0106"].action is Action.RECOVER and found["PO-2026-0106"].amount == Decimal("750.00")
    assert found["PO-2026-0103"].action is Action.RECOVER and found["PO-2026-0103"].amount == Decimal("50.00")
    assert found["PO-2026-0108"].action is Action.ASK_BANK
    assert found["PO-2026-0116"].action is Action.WAIT  # paid a day before the statement ends
    assert found["PO-2026-0107"].action is Action.CONFIRM
    assert any(a.action is Action.INVESTIGATE and a.amount == Decimal("3333.00") for a in actions)
    assert any(a.action is Action.FIX_FILE and "letter O" in a.what_happened for a in actions)


def test_damaged_statement_never_asks_to_pay_or_recover(tmp_path):
    scenarios.scenario_broken_statement(tmp_path, random.Random("05_broken_statement"))
    actions, bank = actions_for(tmp_path / "payout_register.xlsx", tmp_path / "bank_statement.xlsx")

    assert {a.action for a in actions} == {Action.FIX_FILE}
    assert any(n.reason.startswith("Row 8 is an exact copy of row 7") for n in bank.noise)
    linked = [a for a in actions if a.payout_id]
    assert len(linked) == 3 and all("Do NOT pay again" in a.next_step for a in linked)
    assert any("drops by exactly" in a.what_happened for a in linked)  # the row the export dropped
    assert sum("could not be read" in a.what_happened for a in linked) == 2  # the two corrupt rows


def test_balance_gap_from_an_unreadable_row_is_not_a_missing_row(xlsx):
    from tests.test_parsing import bank_rows
    path = xlsx(bank_rows(
        ["03-02-2026", None, "NEFT/A", "R1", "1,000.00", None, "9,000.00"],
        ["31/02/2026", None, "NEFT/B", "R2", "500.00", None, "8,500.00"],
        ["05-02-2026", None, "NEFT/C", "R3", "100.00", None, "8,400.00"],
    ))
    bank = parse_bank_statement(path)
    assert bank.balance_check.status == "VERIFIED"
    assert [i.code for i in bank.issues if i.code.startswith("BALANCE")] == ["BALANCE_GAP_UNREADABLE_ROW"]


def test_real_double_payment_is_still_a_double_payment(xlsx):
    from tests.test_parsing import bank_rows
    path = xlsx(bank_rows(
        ["03-02-2026", None, "IMPS PO-1001", "R1", "500.00", None, "9,500.00"],
        ["03-02-2026", None, "IMPS PO-1001", "R1", "500.00", None, "9,000.00"],  # balance moved again
    ))
    bank = parse_bank_statement(path)
    assert len(bank.rows) == 2 and any(i.code == "DUPLICATE_BANK_ROW" for i in bank.issues)
