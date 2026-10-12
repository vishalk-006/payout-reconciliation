from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.layout.models import ColumnAssignment, DetectedLayout  # noqa: E402
from app.layout.store import LayoutStore  # noqa: E402
from app.models import BankTxn, Direction, Payout, PayoutStatus  # noqa: E402

REGISTER_HEADER = ["Payout ID", "Employee ID", "Employee Name", "Bank Account (masked)", "IFSC", "Amount",
                   "Payout Date", "Claim IDs", "Status"]
BANK_HEADER = ["Txn Date", "Value Date", "Narration", "Cheque/Ref No", "Debit", "Credit", "Balance"]


def write_xlsx(path: Path, rows: list[list]) -> Path:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


@pytest.fixture
def xlsx(tmp_path):
    counter = iter(range(1000))

    def make(rows: list[list], name: str | None = None) -> Path:
        return write_xlsx(tmp_path / (name or f"f{next(counter)}.xlsx"), rows)
    return make


def payout(pid="PO-1001", name="Rahul Kumar", amount="1000.00", on=date(2026, 2, 3), status=PayoutStatus.PAID,
           account="XXXXXX4521", row=2) -> Payout:
    digits = "".join(c for c in account if c.isdigit())
    return Payout(pid, "E1", name, account, digits[-4:] if len(digits) >= 4 else None, "HDFC0001234",
                  Decimal(amount), on, ["CL-1"], status, status.value, row)


def txn(narration="NEFT/CHRONON", amount="1000.00", on=date(2026, 2, 3), direction=Direction.DEBIT, row=10,
        reference="") -> BankTxn:
    return BankTxn(row, on, on, narration, reference, direction, Decimal(amount), None)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """No test writes into the project's storage/ or reaches a real LLM."""
    monkeypatch.setenv("RECON_STORAGE_DIR", str(tmp_path / "storage"))
    for var in ("LLM_PROVIDER", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def store(tmp_path) -> LayoutStore:
    return LayoutStore(learned_dir=tmp_path / "learned_layouts")


def answer(sheet: str, header_row: int, columns: dict[str, str], date_order="DMY", negative="NOT_APPLICABLE",
           expected=True, notes="test layout") -> DetectedLayout:
    return DetectedLayout(is_expected_kind=expected, sheet=sheet, header_row=header_row,
                          columns=[ColumnAssignment(field=f, header=h) for f, h in columns.items()],
                          date_order=date_order, negative_amount_means=negative, notes=notes)


class FakeDetector:
    """Stands in for an LLM: returns the given answers in order (the last one repeats)."""
    name = "fake"
    model = "test-model"

    def __init__(self, *answers: DetectedLayout):
        self.answers = list(answers)
        self.requests = []

    def detect(self, request):
        self.requests.append(request)
        return self.answers[min(len(self.requests), len(self.answers)) - 1]


class ExplodingDetector:
    name = "exploding"
    model = "none"

    def detect(self, request):
        raise AssertionError("the LLM must not be called for a known layout")
