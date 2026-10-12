"""Layout cache. A layout is found by fingerprinting the top rows of a file and
looking for a known header row, so a known format never reaches the LLM.

  app/layouts/*.json         seeded: shipped with the service, reviewed by a person
  storage/layouts/*.json     learned: written after an LLM layout passed validation
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..reader import RawRow
from ..storage import storage_root
from .models import SEEDED, HeaderMatch, Layout, LayoutError, bind, fingerprint, header_cells

SEEDED_DIR = Path(__file__).resolve().parent.parent / "layouts"
SCAN_ROWS = 40


class LayoutStore:
    def __init__(self, seeded_dir: Path | None = None, learned_dir: Path | None = None):
        self.seeded_dir = seeded_dir or SEEDED_DIR
        self.learned_dir = learned_dir or storage_root() / "layouts"

    def all(self, kind: str | None = None) -> list[Layout]:
        layouts = []
        for folder in (self.seeded_dir, self.learned_dir):  # seeded first: a person reviewed those
            if folder.exists():
                for path in sorted(folder.glob("*.json")):
                    layout = Layout.from_dict(json.loads(path.read_text()))
                    if kind is None or layout.kind == kind:
                        layouts.append(layout)
        return layouts

    def find(self, sheets: dict[str, list[RawRow]], kind: str) -> HeaderMatch | None:
        index: dict[str, Layout] = {}
        for layout in self.all(kind):
            index.setdefault(layout.fingerprint, layout)
        if not index:
            return None
        for sheet, rows in sheets.items():
            for i, row in enumerate(rows[:SCAN_ROWS]):
                cells = header_cells(row)
                if len(cells) >= 2 and (layout := index.get(fingerprint(cells))):
                    try:
                        return bind(layout, sheet, rows, i)
                    except LayoutError:
                        continue
        return None

    def save(self, layout: Layout) -> Path:
        return _write_atomic(self.learned_dir / f"{layout.id}.json", layout.to_dict())

    def approve(self, layout_id: str) -> Path:
        """A person checked a learned layout: promote it to the reviewed (seeded) set."""
        learned = self.learned_dir / f"{layout_id}.json"
        if not learned.exists():
            raise LayoutError(f"No learned layout '{layout_id}' in {self.learned_dir}")
        layout = Layout.from_dict(json.loads(learned.read_text()))
        layout.notes = f"{layout.notes} (identified by {layout.source}; approved by a reviewer)".strip()
        layout.source, layout.verified = SEEDED, True
        path = _write_atomic(self.seeded_dir / f"{layout_id}.json", layout.to_dict())
        learned.unlink()
        return path


def _write_atomic(path: Path, data: dict) -> Path:
    """Readers never see a half-written file, even with concurrent reconciliations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return path
