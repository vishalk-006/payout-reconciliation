"""Local file store kept with the service: storage/uploads/<file_id>/ and storage/reports/."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


def storage_root() -> Path:
    return Path(os.environ.get("RECON_STORAGE_DIR", Path(__file__).resolve().parent.parent / "storage"))


@dataclass
class StoredFile:
    file_id: str
    original_filename: str
    stored_name: str
    sha256: str
    size_bytes: int
    uploaded_at: str
    detected_type: str | None = None


class FileStore:
    def __init__(self, root: Path | None = None):
        self.root = root or storage_root()
        self.uploads = self.root / "uploads"
        self.reports = self.root / "reports"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.reports.mkdir(parents=True, exist_ok=True)

    def find_by_hash(self, sha256: str) -> StoredFile | None:
        for meta in self.uploads.glob("*/meta.json"):
            data = json.loads(meta.read_text())
            if data["sha256"] == sha256:
                return StoredFile(**data)
        return None

    def save_upload(self, content: bytes, original_filename: str) -> tuple[StoredFile, bool]:
        """Uploading the same bytes twice returns the existing file_id (created=False)."""
        digest = hashlib.sha256(content).hexdigest()
        if existing := self.find_by_hash(digest):
            return existing, False
        file_id = uuid.uuid4().hex
        suffix = Path(original_filename).suffix.lower()
        folder = self.uploads / file_id
        folder.mkdir(parents=True)
        stored = StoredFile(file_id, Path(original_filename).name, f"original{suffix}", digest, len(content),
                            datetime.now(timezone.utc).isoformat(timespec="seconds"))
        (folder / stored.stored_name).write_bytes(content)
        self._write_meta(stored)
        return stored, True

    def path_of(self, stored: StoredFile) -> Path:
        return self.uploads / stored.file_id / stored.stored_name

    def update(self, stored: StoredFile) -> None:
        self._write_meta(stored)

    def get(self, file_id: str) -> StoredFile | None:
        if not re.fullmatch(r"[0-9a-f]{32}", file_id or ""):
            return None
        meta = self.uploads / file_id / "meta.json"
        return StoredFile(**json.loads(meta.read_text())) if meta.exists() else None

    def report_path(self, report_id: str) -> Path | None:
        if not re.fullmatch(r"[0-9a-f]{32}", report_id or ""):
            return None
        return self.reports / f"{report_id}.xlsx"

    def _write_meta(self, stored: StoredFile) -> None:
        (self.uploads / stored.file_id / "meta.json").write_text(json.dumps(asdict(stored), indent=2))
