"""Pipeline: parse both files -> match -> write report. Each step is a plain function,
so it can later be wrapped as a Temporal activity without changes."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .cleaners import DateFormatError
from .layout.models import LayoutError
from .layout.providers import LayoutDetector
from .layout.spec import KINDS
from .layout.store import LayoutStore
from .matching import DEFAULT_CONFIG, MatchConfig, reconcile
from .parse_bank import parse_bank_statement
from .parse_payouts import parse_payout_register
from .reader import ReadError, read_sheets
from .report import ReportInput, build_report

INPUT_ERRORS = (ReadError, LayoutError, DateFormatError)


@dataclass
class ReconOutcome:
    report_id: str
    report_path: Path
    summary: dict[str, Any]


def run_reconciliation(payout_path: Path, bank_path: Path, reports_dir: Path, *, payout_name: str | None = None,
                       bank_name: str | None = None, llm_provider: str | None = None,
                       store: LayoutStore | None = None, detector: LayoutDetector | None = None,
                       config: MatchConfig = DEFAULT_CONFIG) -> ReconOutcome:
    payouts = parse_payout_register(payout_path, payout_name, store=store, detector=detector, provider=llm_provider)
    bank = parse_bank_statement(bank_path, bank_name, store=store, detector=detector, provider=llm_provider)
    result = reconcile(payouts.rows, bank.rows, config)
    report_id = uuid.uuid4().hex
    report_path = reports_dir / f"{report_id}.xlsx"
    summary = build_report(ReportInput(payouts, bank, result, datetime.now()), report_path)
    return ReconOutcome(report_id, report_path, summary)


def detect_file_type(path: Path, store: LayoutStore | None = None) -> tuple[str | None, str | None]:
    """(kind, layout id) for a known layout. Never calls an LLM: new layouts are identified during reconciliation."""
    sheets = read_sheets(path)
    store = store or LayoutStore()
    for kind in KINDS:
        if hit := store.find(sheets, kind):
            return kind, hit.layout.id
    return None, None
