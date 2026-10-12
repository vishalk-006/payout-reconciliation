import json
import re
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.layout.models import DetectedLayout, LayoutError, WrongFileKind, header_from_detection
from app.layout.providers import DetectionRequest, ProviderError, ProviderNotConfigured, get_detector, register
from app.layout.providers.base import build_user_prompt, layout_json_schema
from app.layout.providers.claude import ClaudeLayoutDetector
from app.layout.providers.gemini import GeminiLayoutDetector
from app.layout.sample import render_sample
from app.layout.spec import BANK_STATEMENT
from app.parse_bank import parse_bank_statement
from app.reader import RawRow, read_sheets
from tests.conftest import FakeDetector, answer

HEADER = ["Posted", "Details", "Withdrawal Amt.", "Deposit Amt.", "Closing Bal"]
GOOD = {"txn_date": "Posted", "narration": "Details", "debit": "Withdrawal Amt.", "credit": "Deposit Amt.",
        "balance": "Closing Bal"}
SWAPPED = {**GOOD, "debit": "Deposit Amt.", "credit": "Withdrawal Amt."}


def statement(xlsx, rows=None):
    return xlsx([["MY BANK"], [], HEADER, *(rows or [
        ["03-02-2026", "NEFT RAHUL", "1,000.00", None, "9,000.00"],
        ["04-02-2026", "IMPS PRIYA", "500.00", None, "8,500.00"],
        ["05-02-2026", "INTEREST", None, "100.00", "8,600.00"],
        ["06-02-2026", "NEFT AMIT", "200.00", None, "8,400.00"],
        ["07-02-2026", "NEFT NEHA", "300.00", None, "8,100.00"],
    ])])


# --------------------------------------------------------------------------- resolver: validate, retry, cache

def test_swapped_columns_are_caught_by_balance_check_and_corrected(xlsx, store):
    llm = FakeDetector(answer("Sheet", 3, SWAPPED), answer("Sheet", 3, GOOD))
    res = parse_bank_statement(statement(xlsx), store=store, detector=llm)
    assert len(llm.requests) == 2
    assert "running balance" in llm.requests[1].feedback[0]  # the LLM is told exactly what failed
    assert res.layout.verified and res.layout.attempts == 2 and res.balance_check.status == "VERIFIED"
    assert len(store.all("bank_statement")) == 2  # seeded + newly learned


def test_made_up_header_is_rejected_and_retried(xlsx, store):
    llm = FakeDetector(answer("Sheet", 3, {**GOOD, "reference": "UTR Number"}), answer("Sheet", 3, GOOD))
    res = parse_bank_statement(statement(xlsx), store=store, detector=llm)
    assert "'UTR Number'" in llm.requests[1].feedback[0] and res.layout.verified


def test_clearly_wrong_layout_stops_with_error_and_is_not_saved(xlsx, store):
    llm = FakeDetector(answer("Sheet", 3, SWAPPED))
    with pytest.raises(LayoutError, match="after 2 attempts with fake:test-model"):
        parse_bank_statement(statement(xlsx), store=store, detector=llm)
    assert len(store.all("bank_statement")) == 1  # only the seeded one


def test_correct_layout_on_imperfect_file_continues_unverified(xlsx, store):
    rows = [["03-02-2026", "NEFT A", "1,000.00", None, "9,000.00"],
            ["04-02-2026", "NEFT B", "500.00", None, "8,000.00"],  # one break: a row is missing before this
            ["05-02-2026", "NEFT C", "100.00", None, "7,900.00"],
            ["06-02-2026", "NEFT D", "100.00", None, "7,800.00"],
            ["07-02-2026", "NEFT E", "100.00", None, "7,700.00"],
            ["08-02-2026", "NEFT F", "100.00", None, "7,600.00"]]
    llm = FakeDetector(answer("Sheet", 3, GOOD))
    res = parse_bank_statement(statement(xlsx, rows), store=store, detector=llm)
    assert not res.layout.verified and res.layout.problems and res.balance_check.status == "BROKEN"
    assert len(store.all("bank_statement")) == 1  # unverified layouts are never cached


def test_wrong_kind_of_file_fails_immediately(xlsx, store):
    llm = FakeDetector(answer("Sheet", 3, GOOD, expected=False, notes="This is a payout register."))
    with pytest.raises(WrongFileKind, match="payout register"):
        parse_bank_statement(statement(xlsx), store=store, detector=llm)
    assert len(llm.requests) == 1


# --------------------------------------------------------------------------- structural checks

def sheets_with(*rows):
    return {"S": [RawRow(i + 1, list(r)) for i, r in enumerate(rows)]}


@pytest.mark.parametrize("columns, message", [
    ({**GOOD, "salary": "Details"}, "'salary' is not a valid field"),
    ({"txn_date": "Posted", "narration": "Details"}, re.escape("Required fields not mapped: credit+debit or amount")),
    ({**GOOD, "amount": "Deposit Amt."}, "already used for 'credit'"),
])
def test_structural_validation_messages(columns, message):
    sheets = sheets_with(HEADER, ["03-02-2026", "x", "1", None, "1"])
    with pytest.raises(LayoutError, match=message):
        header_from_detection(sheets, answer("S", 1, columns), BANK_STATEMENT, "t")


def test_wrong_sheet_or_row_is_rejected():
    sheets = sheets_with(HEADER)
    with pytest.raises(LayoutError, match="Sheet 'Nope'"):
        header_from_detection(sheets, answer("Nope", 1, GOOD), BANK_STATEMENT, "t")
    with pytest.raises(LayoutError, match="Row 9"):
        header_from_detection(sheets, answer("S", 9, GOOD), BANK_STATEMENT, "t")


# --------------------------------------------------------------------------- what is sent to the LLM

def test_sample_masks_personal_data_but_keeps_layout_clues(xlsx):
    path = xlsx([["Account No: 50100123456789", "Customer: RAHUL KUMAR"], HEADER,
                 [datetime(2026, 2, 3), "NEFT/RAHUL KUMAR/XX7788", "4,250.00", None, "1,00,000.00"],
                 [None, "Opening Balance", None, None, "2,50,000.00"]])
    text = render_sample(read_sheets(path))
    assert "50100123456789" not in text and "RAHUL" not in text.split("R3:")[1]
    assert "Withdrawal Amt." in text and "Closing Bal" in text  # header texts intact
    assert "4,250.00" in text and "1,00,000.00" in text and "<date 2026-02-03>" in text  # value shapes intact
    assert "NEFT" in text and "Opening Balance" in text  # banking keywords intact


def test_prompt_lists_fields_and_feedback():
    req = DetectionRequest(BANK_STATEMENT, "R1: [x]", ["balance broke at row 7"])
    prompt = build_user_prompt(req)
    assert "txn_date (required)" in prompt and "credit + debit; amount" in prompt
    assert "REJECTED" in prompt and "balance broke at row 7" in prompt


def test_schema_has_no_refs_for_providers():
    assert "$ref" not in json.dumps(layout_json_schema())


# --------------------------------------------------------------------------- provider adapters (SDKs mocked)

GOOD_LAYOUT = answer("Sheet", 3, GOOD)


class FakeGeminiModels:
    def __init__(self, text=None, exc=None):
        self.text, self.exc, self.calls = text, exc, []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return SimpleNamespace(text=self.text)


def test_gemini_adapter_parses_structured_output():
    models = FakeGeminiModels(text=GOOD_LAYOUT.model_dump_json())
    det = GeminiLayoutDetector(model="gemini-3.8-flash", client=SimpleNamespace(models=models))
    out = det.detect(DetectionRequest(BANK_STATEMENT, "R1: [x]"))
    assert out == GOOD_LAYOUT
    call = models.calls[0]
    assert call["model"] == "gemini-3.8-flash" and call["config"].response_mime_type == "application/json"
    assert call["config"].response_json_schema["properties"]["date_order"]["enum"] == ["DMY", "MDY", "YMD", "UNKNOWN"]


@pytest.mark.parametrize("models, message", [
    (FakeGeminiModels(text=""), "no content"),
    (FakeGeminiModels(text='{"sheet": 1}'), "does not match"),
    (FakeGeminiModels(exc=ConnectionError("down")), "request failed"),
])
def test_gemini_adapter_failures_become_provider_errors(models, message):
    det = GeminiLayoutDetector(client=SimpleNamespace(models=models))
    with pytest.raises(ProviderError, match=message):
        det.detect(DetectionRequest(BANK_STATEMENT, "R1: [x]"))


def test_gemini_without_key_is_not_configured():
    with pytest.raises(ProviderNotConfigured, match="GEMINI_API_KEY"):
        get_detector("gemini")


class FakeClaudeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def claude_with(response):
    messages = FakeClaudeMessages(response)
    return ClaudeLayoutDetector(client=SimpleNamespace(beta=SimpleNamespace(messages=messages))), messages


def test_claude_adapter_parses_structured_output():
    det, messages = claude_with(SimpleNamespace(stop_reason="end_turn", parsed_output=GOOD_LAYOUT))
    assert det.detect(DetectionRequest(BANK_STATEMENT, "R1: [x]")) == GOOD_LAYOUT
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["output_format"] is DetectedLayout
    assert call["fallbacks"] == "default" and call["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize("stop, parsed, message", [
    ("refusal", None, "declined"), ("max_tokens", None, "cut off"), ("end_turn", None, "no structured"),
])
def test_claude_adapter_failures_become_provider_errors(stop, parsed, message):
    det, _ = claude_with(SimpleNamespace(stop_reason=stop, parsed_output=parsed))
    with pytest.raises(ProviderError, match=message):
        det.detect(DetectionRequest(BANK_STATEMENT, "R1: [x]"))


def test_new_provider_plugs_in_through_the_registry(xlsx, store, monkeypatch):
    """Extensibility: a provider is a class with detect() plus one @register line."""
    llm = FakeDetector(answer("Sheet", 3, GOOD))
    register("my-llm")(lambda: llm)
    monkeypatch.setenv("LLM_PROVIDER", "my-llm")
    res = parse_bank_statement(statement(xlsx), store=store)
    assert res.layout.source == "fake:test-model" and len(llm.requests) == 1


# --------------------------------------------------------------------------- review hardening

NO_BALANCE = ["Posted", "Details", "Withdrawal Amt.", "Deposit Amt."]
NO_BALANCE_MAP = {k: v for k, v in GOOD.items() if k != "balance"}


def no_balance_statement(xlsx):
    return xlsx([NO_BALANCE, ["03-02-2026", "NEFT A", "1,000.00", None], ["04-02-2026", "NEFT B", None, "5.00"]])


def test_unprovable_layout_is_cached_as_pending_review_until_approved(xlsx, store):
    from tests.conftest import ExplodingDetector
    first = parse_bank_statement(no_balance_statement(xlsx), store=store,
                                 detector=FakeDetector(answer("Sheet", 1, NO_BALANCE_MAP)))
    assert not first.layout.verified and "approve" in first.layout.problems[0]
    again = parse_bank_statement(no_balance_statement(xlsx), store=store, detector=ExplodingDetector())
    assert not again.layout.verified and not again.layout.llm_called  # reused, still flagged
    store.seeded_dir = store.learned_dir.parent / "seeded"
    store.approve(first.layout.id)
    approved = parse_bank_statement(no_balance_statement(xlsx), store=store, detector=ExplodingDetector())
    assert approved.layout.verified and approved.layout.source == "seeded"


def test_new_register_layout_always_needs_review(xlsx, store):
    from app.parse_payouts import parse_payout_register
    path = xlsx([["Ref", "Who", "Paid", "On", "State"], ["P-00001", "A", "100", "03-02-2026", "PAID"]])
    llm = FakeDetector(answer("Sheet", 1, {"payout_id": "Ref", "employee_name": "Who", "amount": "Paid",
                                           "payout_date": "On", "status": "State"}))
    res = parse_payout_register(path, store=store, detector=llm)
    assert len(res.rows) == 1 and not res.layout.verified  # nothing in a register proves the mapping


def test_stale_learned_layout_is_identified_again(xlsx, store):
    # Learn a layout, then the bank swaps what the two amount columns mean while keeping their names.
    parse_bank_statement(statement(xlsx), store=store, detector=FakeDetector(answer("Sheet", 3, GOOD)))
    swapped_meaning = [[r[0], r[1], r[3], r[2], r[4]] for r in [
        ["03-02-2026", "NEFT A", "1,000.00", None, "9,000.00"], ["04-02-2026", "NEFT B", "500.00", None, "8,500.00"],
        ["05-02-2026", "NEFT C", "100.00", None, "8,400.00"], ["06-02-2026", "NEFT D", "100.00", None, "8,300.00"],
        ["07-02-2026", "NEFT E", "100.00", None, "8,200.00"]]]
    llm = FakeDetector(answer("Sheet", 3, SWAPPED))
    res = parse_bank_statement(statement(xlsx, swapped_meaning), store=store, detector=llm)
    assert len(llm.requests) == 1 and res.layout.llm_called and res.balance_check.status == "VERIFIED"


def test_reviewed_layout_with_unreadable_file_still_reports_rows(xlsx, store):
    from tests.conftest import BANK_HEADER, ExplodingDetector
    path = xlsx([BANK_HEADER, ["03-02-2026", None, "NEFT A", "R1", "12O0", None, "1"]])
    res = parse_bank_statement(path, store=store, detector=ExplodingDetector())
    assert len(res.rejected) == 1 and not res.layout.verified  # flagged, not hidden behind an error


def test_other_sheets_with_data_are_reported(tmp_path, store):
    from openpyxl import Workbook

    from tests.conftest import BANK_HEADER
    wb = Workbook()
    wb.active.append(BANK_HEADER)
    wb.active.append(["03-02-2026", None, "NEFT A", "R1", "10", None, "90"])
    wb.create_sheet("Page 2").append(["04-02-2026", None, "NEFT B", "R2", "10", None, "80"])
    wb.save(tmp_path / "two_sheets.xlsx")
    res = parse_bank_statement(tmp_path / "two_sheets.xlsx", store=store)
    assert any(i.code == "UNREAD_SHEET" and "Page 2" in i.message for i in res.issues)


def test_row_accounting_is_checked_without_assert():
    from app.models import RowAccounting, RowAccountingError
    with pytest.raises(RowAccountingError):
        RowAccounting(total_rows=5, header=1, parsed=3).ensure_balanced("f.xlsx")
