"""Provider-neutral interface for layout detection.

A provider only has to turn (instructions, sample) into a DetectedLayout.
The prompt, the schema and all validation are shared, so adding OpenAI or any
other API is one small adapter file plus a @register line.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Protocol

from ..models import DetectedLayout
from ..spec import KindSpec


class ProviderError(RuntimeError):
    """The LLM call itself failed (network, auth, refusal, unusable output)."""


class ProviderNotConfigured(ProviderError):
    """No provider selected, unknown provider, missing SDK or missing credentials."""


@dataclass
class DetectionRequest:
    spec: KindSpec
    sample: str
    feedback: list[str] = field(default_factory=list)  # why the previous answer was rejected, for a retry


class LayoutDetector(Protocol):
    name: str
    model: str

    def detect(self, request: DetectionRequest) -> DetectedLayout: ...


SYSTEM_PROMPT = """You identify the table layout of spreadsheet exports so that a deterministic parser can read them.

You are given the first rows of each sheet. Each line is one row: "R<row number>: [cell] | [cell] | ...".
Personal data has been masked (letters replaced by 'x', long digit runs shortened). Dates and amounts are shown as written.

Find the row that holds the column headers of the main transaction table (it is often below a title block, account
details or blank rows) and map its columns to the canonical fields you are given.

Rules:
- Copy header texts exactly as they appear in the header row. Never invent a header.
- Map a column only when you are confident. Leave optional fields out rather than guess.
- Use either debit+credit (two columns) or amount (one column), never both.
- date_order: read it from the date VALUES. A day above 12 settles it; otherwise use other evidence (a statement
  period line, rows being in date order). Answer UNKNOWN if the values cannot tell you.
- negative_amount_means: only for a single signed amount column without a Dr/Cr column; otherwise NOT_APPLICABLE.
- Set is_expected_kind to false only if the file is clearly a different kind of document.
- notes: one or two short sentences an auditor can check."""


def build_user_prompt(req: DetectionRequest) -> str:
    fields = "\n".join(
        f"- {f.name}{' (required)' if f.name in req.spec.required else ''}: {f.description}" for f in req.spec.fields)
    any_rule = ""
    if req.spec.required_any:
        any_rule = "\nAt least one of these groups must be fully mapped: " + "; ".join(
            " + ".join(sorted(g)) for g in req.spec.required_any) + "."
    feedback = ""
    if req.feedback:
        feedback = ("\n\nYOUR PREVIOUS ANSWER WAS CHECKED AGAINST THE FULL FILE AND REJECTED:\n- "
                    + "\n- ".join(req.feedback) + "\nCorrect the layout.")
    return (f"Expected document: {req.spec.title}.\n\nCanonical fields:\n{fields}{any_rule}\n\n"
            f"Sample:\n{req.sample}{feedback}")


def inline_schema(schema: dict) -> dict:
    """Resolve $ref/$defs so providers with partial JSON-schema support get a flat schema."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node
    return walk(schema)


def layout_json_schema() -> dict:
    return inline_schema(DetectedLayout.model_json_schema())
