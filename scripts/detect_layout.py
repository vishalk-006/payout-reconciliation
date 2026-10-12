"""Try layout detection on one file, outside the API.

    GEMINI_API_KEY=... .venv/bin/python scripts/detect_layout.py statement.xlsx
    .venv/bin/python scripts/detect_layout.py statement.xlsx --provider claude --kind bank_statement
    .venv/bin/python scripts/detect_layout.py statement.xlsx --show-sample   # what would be sent, no LLM call

A layout that passes validation is saved to storage/layouts/ and reused from then on.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.layout.sample import render_sample  # noqa: E402
from app.parse_bank import parse_bank_statement  # noqa: E402
from app.parse_payouts import parse_payout_register  # noqa: E402
from app.reader import read_sheets  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", type=Path)
    ap.add_argument("--kind", choices=["bank_statement", "payout_register"], default="bank_statement")
    ap.add_argument("--provider", help="gemini (default) | claude | any registered provider")
    ap.add_argument("--show-sample", action="store_true", help="print the masked sample and exit")
    args = ap.parse_args()

    if args.show_sample:
        print(render_sample(read_sheets(args.file)))
        return
    parse = parse_bank_statement if args.kind == "bank_statement" else parse_payout_register
    res = parse(args.file, provider=args.provider)
    lay = res.layout
    print(f"layout   {lay.id}  source={lay.source}  verified={lay.verified}  llm_called={lay.llm_called} "
          f"attempts={lay.attempts}")
    print(f"notes    {lay.notes}")
    if lay.problems:
        print("problems " + " | ".join(lay.problems))
    print(f"header   row {res.header_row} of sheet '{res.sheet}', dates {res.date_format}")
    print(f"rows     parsed={len(res.rows)} rejected={len(res.rejected)} skipped={len(res.noise)}")
    if hasattr(res, "balance_check"):
        print(f"balance  {res.balance_check.status}: {res.balance_check.message}")


if __name__ == "__main__":
    main()
