# Payout Reconciliation

Reconciles Chronon's **payout register** (File A) against the company **bank statement** (File B)
and produces an Excel report finance can act on.

## Run

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/make_sample_data.py          # optional: messy sample files in sample_data/
.venv/bin/uvicorn app.main:app --reload               # API docs with upload buttons: http://localhost:8000/docs
.venv/bin/python -m pytest -q
```

## API

| Step | Call | Result |
|---|---|---|
| 1. Upload each file | `POST /api/files` (multipart `file`) | `file_id`; `detected_type`/`layout_id` when the layout is already known (no LLM call on upload). Same bytes uploaded twice return the same id. |
| 2. Reconcile | `POST /api/reconciliations` `{"payout_file_id": "...", "bank_file_id": "...", "llm_provider": null}` | The Excel report (download). Headers: `X-Report-Id`, `X-Totals-Tie`, `X-Bank-Balance-Check`, `X-Layouts-Verified`, `X-Needs-Review` |
| Re-download | `GET /api/reports/{report_id}` | The same report |

```bash
P=$(curl -s -F file=@sample_data/payout_register.xlsx localhost:8000/api/files | jq -r .file_id)
B=$(curl -s -F file=@sample_data/bank_statement.xlsx  localhost:8000/api/files | jq -r .file_id)
curl -s -o report.xlsx -H 'Content-Type: application/json' \
     -d "{\"payout_file_id\":\"$P\",\"bank_file_id\":\"$B\"}" localhost:8000/api/reconciliations
```

Files are kept with the service in `storage/` (`RECON_STORAGE_DIR` to override).

## How it works

```
reader.py      raw cells + real Excel row numbers (no pandas type guessing)
layout/        where is the table and which column is which (cache, else an LLM) - see below
parse_*.py     strict cleaning; every row ends up parsed, rejected (with reason) or skipped (with reason)
classify.py    bank row categories + narration clues (keywords in app/rules/classification.yaml)
matching.py    deterministic rules R1–R7 + return linking; each result says which rule and why
report.py      Summary (tie-out), Needs Review, Matched, All Payouts, All Bank Rows, Data Quality
```

**Correctness guards**
- Amounts are `Decimal`; only known shapes are accepted (`1,00,000.00`, `₹1,200`, `(500)`, `950 Dr`). `12O0` or `1.200,50` are rejected, never "fixed".
- One date format is chosen per column from all values. If dd/mm vs mm/dd is still ambiguous and the layout doesn't say, reconciliation stops instead of guessing.
- The bank's **running balance** is replayed row by row: a missing, duplicated or misread row is reported with its row number.
- **Row accounting**: rows in sheet = above header + header + skipped + parsed + rejected.
- The summary shows counts and amounts per outcome **tying back to each file's totals**, plus opening − debits + credits = closing.

**Matching rules** (strongest first; weak rules only accept a pair that is unique in both directions)

| Rule | Evidence | Outcome |
|---|---|---|
| R1 | Payout ID in narration/ref, amount equal | Matched (High) |
| R2 | Payout ID, amount differs | Amount mismatch |
| R3 | Several Payout IDs in one debit | Batch matched / mismatch |
| R4 | Same amount + date window (−1/+5 days) + account last-4 and/or name | Matched (High/Medium) |
| R5 | Same amount + date window only | Probable – confirm |
| R6 | Account/full name + date window, amount differs | Amount mismatch |
| R7 | One debit = all of a day's payouts | Probable – confirm |
| RT | Return credit linked by ID, reference, or amount + name | Returned |

Payout status is checked too: a FAILED payout with a bank debit is a status conflict; a PENDING one with no debit is "not yet debited".

## File layouts: deterministic cache + LLM for new formats

The only job given to an LLM is **finding the table**: which row is the header and which column is the
date, narration, debit, credit, balance... Every value is still read by the strict deterministic
cleaners, and matching never involves an LLM.

```
file ──► header fingerprint known? ── yes ──► parse (no LLM call)
                 │ no
                 ▼
         LLM (masked sample of the top rows) ──► JSON layout (strict schema)
                 ▼
         checks: every named column exists in that row; whole file parses;
                 running balance holds; few unreadable rows
                 ├─ pass ──► save to storage/layouts/ (reused next time) ──► parse
                 ├─ fail ──► retry once, telling the LLM exactly what failed
                 └─ still failing ──► minor: continue, flagged "layout not verified" (P1, not cached)
                                      clearly wrong: stop with an error
```

| Setting | Default | Meaning |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` or `claude` (or any registered provider) |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | –, `gemini-3.8-flash` | Google Gemini (paid tier: data not used for training) |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` | –, `claude-opus-5-5` | Anthropic Claude |

- **Seeded layouts** (`app/layouts/*.json`) ship with the service and are reviewed by a person: Chronon's
  register and the current bank. They never call an LLM.
- **Learned layouts** (`storage/layouts/`) are re-checked on every file. One that clearly stops fitting is
  identified again. A layout the file cannot prove (no balance column, or any payout register) is saved as
  *pending review*: reused, but flagged P1 in every report until approved:
  `.venv/bin/python scripts/approve_layout.py` (list) / `scripts/approve_layout.py <layout id>` (approve).
- **What is sent:** at most the first 40 rows per sheet; names masked (`Rxxxx Kxxxx`), long digit runs
  shortened; header texts, date/amount shapes and banking keywords kept. Preview with
  `scripts/detect_layout.py FILE --show-sample`.
- **Adding a provider** (e.g. OpenAI): a class with `name`, `model` and `detect(request) -> DetectedLayout`
  in `app/layout/providers/`, plus `@register("openai")` and an import in `providers/__init__.py`.
  The prompt (`base.py`), JSON schema, validation and caching are shared.
- Try one file: `GEMINI_API_KEY=... .venv/bin/python scripts/detect_layout.py statement.xlsx`.
