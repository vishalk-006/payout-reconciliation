"""HTTP API.

  POST /api/files              upload one Excel/CSV file -> file_id
  POST /api/reconciliations    {payout_file_id, bank_file_id, llm_provider?} -> Excel report (download)
  GET  /api/reports/{id}       download a report again

Interactive docs (with upload buttons) at /docs.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .layout.providers import ProviderError, ProviderNotConfigured, available
from .reader import SUPPORTED_SUFFIXES, ReadError
from .service import INPUT_ERRORS, detect_file_type, run_reconciliation
from .storage import FileStore

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

app = FastAPI(title="Payout Reconciliation", version="1.0.0")


def store() -> FileStore:
    return FileStore()


class UploadResponse(BaseModel):
    file_id: str
    filename: str
    size_bytes: int
    sha256: str
    uploaded_at: str
    detected_type: str | None = Field(
        description="payout_register | bank_statement when the layout is already known; null = new layout, "
                    "identified by the LLM during reconciliation")
    layout_id: str | None
    already_uploaded: bool


class ReconciliationRequest(BaseModel):
    payout_file_id: str
    bank_file_id: str
    llm_provider: str | None = Field(
        default=None, description=f"LLM used only for files with an unknown layout ({', '.join(available())}); "
                                  f"default from LLM_PROVIDER")


@app.post("/api/files", response_model=UploadResponse, status_code=201)
async def upload_file(file: UploadFile) -> UploadResponse:
    name = file.filename or "upload"
    if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
        raise HTTPException(415, f"Unsupported file type. Upload one of: {', '.join(sorted(SUPPORTED_SUFFIXES))}")
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    if not content:
        raise HTTPException(422, "File is empty")

    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / f"probe{Path(name).suffix.lower()}"
        probe.write_bytes(content)
        try:
            detected, layout_id = detect_file_type(probe)
        except ReadError as e:
            raise HTTPException(422, str(e)) from e

    fs = store()
    stored, created = fs.save_upload(content, name)
    if stored.detected_type != detected:
        stored.detected_type = detected
        fs.update(stored)
    return UploadResponse(file_id=stored.file_id, filename=stored.original_filename, size_bytes=stored.size_bytes,
                          sha256=stored.sha256, uploaded_at=stored.uploaded_at, detected_type=detected,
                          layout_id=layout_id, already_uploaded=not created)


@app.post("/api/reconciliations", response_class=FileResponse,
          responses={200: {"content": {XLSX_MEDIA_TYPE: {}}, "description": "The reconciliation report"}})
def create_reconciliation(req: ReconciliationRequest) -> FileResponse:
    fs = store()
    if req.payout_file_id == req.bank_file_id:
        raise HTTPException(422, "payout_file_id and bank_file_id must be different files")
    payout_file = fs.get(req.payout_file_id)
    bank_file = fs.get(req.bank_file_id)
    missing = [n for n, f in (("payout_file_id", payout_file), ("bank_file_id", bank_file)) if f is None]
    if missing:
        raise HTTPException(404, f"Unknown {', '.join(missing)}. Upload the file first via POST /api/files.")

    try:
        outcome = run_reconciliation(fs.path_of(payout_file), fs.path_of(bank_file), fs.reports,
                                     payout_name=payout_file.original_filename,
                                     bank_name=bank_file.original_filename, llm_provider=req.llm_provider)
    except INPUT_ERRORS as e:
        raise HTTPException(422, str(e)) from e
    except ProviderNotConfigured as e:
        raise HTTPException(503, f"A file has a layout this service has not seen before, and the LLM needed to "
                                 f"identify it is not available: {e}") from e
    except ProviderError as e:
        raise HTTPException(502, f"The LLM call to identify the file layout failed: {e}") from e

    s = outcome.summary
    return FileResponse(
        outcome.report_path, media_type=XLSX_MEDIA_TYPE, filename=f"reconciliation_{outcome.report_id}.xlsx",
        headers={
            "X-Report-Id": outcome.report_id,
            "X-Needs-Review": str(s["needs_review"]),
            "X-Totals-Tie": str(s["payouts"]["ties"] and s["bank_debits"]["ties"] and s["bank_credits"]["ties"]).lower(),
            "X-Bank-Balance-Check": s["balance_check"],
            "X-Layouts-Verified": str(s["layouts_verified"]).lower(),
        },
    )


@app.get("/api/reports/{report_id}", response_class=FileResponse)
def download_report(report_id: str) -> FileResponse:
    path = store().report_path(report_id)
    if path is None or not path.exists():
        raise HTTPException(404, "Report not found")
    return FileResponse(path, media_type=XLSX_MEDIA_TYPE, filename=f"reconciliation_{report_id}.xlsx")
