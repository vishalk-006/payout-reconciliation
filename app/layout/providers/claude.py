"""Anthropic Claude adapter (anthropic SDK). Credentials: ANTHROPIC_API_KEY or an `ant auth login` profile.
Model: CLAUDE_MODEL."""
from __future__ import annotations

import os

from ..models import DetectedLayout
from .base import SYSTEM_PROMPT, DetectionRequest, ProviderError, ProviderNotConfigured, build_user_prompt
from .registry import register

DEFAULT_MODEL = "claude-opus-5-5"


class ClaudeLayoutDetector:
    name = "claude"

    def __init__(self, model: str = DEFAULT_MODEL, client=None):
        import anthropic
        self._anthropic = anthropic
        self.model = model
        if client is None:
            try:
                client = anthropic.Anthropic()
            except Exception as e:
                raise ProviderNotConfigured(f"Claude is not configured: {e}. Set ANTHROPIC_API_KEY.") from e
        self.client = client

    def detect(self, request: DetectionRequest) -> DetectedLayout:
        a = self._anthropic
        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                # On a safety decline, re-run on Anthropic's recommended fallback model instead of failing.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "medium"},
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_prompt(request)}],
                output_format=DetectedLayout,
            )
        except a.APIStatusError as e:
            raise ProviderError(f"Claude API error {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise ProviderError(f"Claude request failed: {e}") from e
        if response.stop_reason == "refusal":
            raise ProviderError("Claude declined the request.")
        if response.stop_reason == "max_tokens":
            raise ProviderError("Claude's answer was cut off (max_tokens).")
        if response.parsed_output is None:
            raise ProviderError("Claude returned no structured layout.")
        return response.parsed_output


@register("claude")
def _make() -> ClaudeLayoutDetector:
    try:
        return ClaudeLayoutDetector(model=os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL))
    except ImportError as e:
        raise ProviderNotConfigured("anthropic is not installed (pip install anthropic).") from e
