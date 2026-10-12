"""Approve a layout an LLM identified, after a person has checked its column mapping.

    .venv/bin/python scripts/approve_layout.py                       # list learned layouts
    .venv/bin/python scripts/approve_layout.py bank_statement-ab12cd34ef56ab78

Approved layouts move to app/layouts/ (reviewed, shipped with the service) and are no longer flagged.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.layout.store import LayoutStore  # noqa: E402


def main() -> None:
    store = LayoutStore()
    if len(sys.argv) < 2:
        learned = [lay for lay in store.all() if lay.source != "seeded"]
        for lay in learned:
            status = "verified" if lay.verified else "PENDING REVIEW"
            mapping = ", ".join(f"{f}={c['header']!r}" for f, c in lay.columns.items())
            print(f"{lay.id}  [{status}]  {lay.source}  {lay.created_at}\n    {mapping}\n    {lay.notes}")
        if not learned:
            print("No learned layouts.")
        return
    print(f"Approved -> {store.approve(sys.argv[1])}")


if __name__ == "__main__":
    main()
