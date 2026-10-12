"""Find the layout of a file, then parse it.

  1. Known layout (header fingerprint in the cache) -> parse -> re-check on this
     file. A learned layout that clearly no longer fits is re-identified; a
     reviewed (seeded) one that no longer fits stops with an error.
  2. Unknown layout -> ask the LLM -> check its answer against the header row
     -> parse the WHOLE file with it -> the parser's checks must pass
     (for a bank statement: the running balance) -> save to the cache.
     A layout the file cannot prove (e.g. no balance column) is saved as
     "pending review": reused, but flagged until a person approves it.
  3. Rejected -> ask again, telling the LLM exactly what failed. After the last
     attempt: minor problems -> continue, flagged and not cached; clearly wrong
     -> stop with an error rather than produce a misleading report.

The LLM only says where things are. Every value is still read by the strict,
deterministic cleaners.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, TypeVar
from collections.abc import Callable

from ..cleaners import DateFormatError
from ..models import LayoutInfo
from ..reader import RawRow
from .models import SEEDED, HeaderMatch, Layout, LayoutError, WrongFileKind, header_from_detection
from .providers import DetectionRequest, LayoutDetector, ProviderError, get_detector
from .sample import render_sample
from .spec import KindSpec
from .store import LayoutStore

MAX_ATTEMPTS = 2
PENDING_REVIEW = ("Saved without proof from the file itself (for example, no balance column to check debits against "
                  "credits). Confirm the column mapping, then approve it: scripts/approve_layout.py {id}")


class ParseResult(Protocol):
    layout: LayoutInfo | None


R = TypeVar("R", bound=ParseResult)
Sheets = dict[str, list[RawRow]]


@dataclass
class Check:
    """How well a layout held up when the whole file was parsed with it."""
    problems: list[str] = field(default_factory=list)
    fatal: bool = False  # the layout is almost certainly wrong, not just the file imperfect
    proven: bool = True  # the file contains evidence that the mapping is right (e.g. the balance chain)


def resolve_and_parse(sheets: Sheets, spec: KindSpec, file_name: str, parse: Callable[[Sheets, HeaderMatch], R],
                      check: Callable[[R], Check], *, store: LayoutStore | None = None,
                      detector: LayoutDetector | None = None, provider: str | None = None) -> R:
    store = store or LayoutStore()
    cached = store.find(sheets, spec.kind)
    if cached is None:
        return _detect_and_parse(sheets, spec, file_name, parse, check, store, detector or get_detector(provider))

    result = parse(sheets, cached)
    outcome = check(result)
    # A reviewed layout is trusted over the file: a bad file is reported (flagged), not re-interpreted.
    if not outcome.fatal or cached.layout.source == SEEDED:
        return _attach(result, cached.layout, outcome, llm_called=False, attempts=0)
    # A learned layout may have gone stale: identify it again (a passing answer overwrites the cached file).
    # If that cannot do better, keep the cached reading, flagged - never hide the file's rows behind an error.
    try:
        return _detect_and_parse(sheets, spec, file_name, parse, check, store, detector or get_detector(provider))
    except (LayoutError, ProviderError):
        return _attach(result, cached.layout, outcome, llm_called=False, attempts=0)


def _detect_and_parse(sheets: Sheets, spec: KindSpec, file_name: str, parse: Callable[[Sheets, HeaderMatch], R],
                      check: Callable[[R], Check], store: LayoutStore, detector: LayoutDetector) -> R:
    source = f"{detector.name}:{detector.model}"
    sample = render_sample(sheets)
    feedback: list[str] = []
    unverified_fallback: tuple[R, Layout, Check, int] | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        answer = detector.detect(DetectionRequest(spec, sample, feedback))
        try:
            header = header_from_detection(sheets, answer, spec, source)
            result = parse(sheets, header)
        except WrongFileKind:
            raise
        except (LayoutError, DateFormatError) as e:
            feedback = [str(e)]
            continue
        outcome = check(result)
        if not outcome.problems:
            header.layout.verified = outcome.proven
            store.save(header.layout)
            return _attach(result, header.layout, outcome, llm_called=True, attempts=attempt)
        feedback = outcome.problems
        if not outcome.fatal:
            unverified_fallback = (result, header.layout, outcome, attempt)

    if unverified_fallback:
        result, layout, outcome, attempt = unverified_fallback
        return _attach(result, layout, outcome, llm_called=True, attempts=attempt)
    raise LayoutError(f"Could not identify the layout of '{file_name}' as a {spec.title} after {MAX_ATTEMPTS} "
                      f"attempts with {source}. Last problem: {' '.join(feedback)}")


def _attach(result: R, layout: Layout, outcome: Check, *, llm_called: bool, attempts: int) -> R:
    problems = list(outcome.problems)
    if not layout.verified and not problems:
        problems.append(PENDING_REVIEW.format(id=layout.id))
    result.layout = LayoutInfo(layout.id, layout.source, layout.verified and not outcome.problems, llm_called,
                               attempts, layout.notes, problems)
    return result
