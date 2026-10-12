"""Google Gemini adapter (google-genai SDK). Credentials: GEMINI_API_KEY. Model: GEMINI_MODEL."""
from __future__ import annotations

import os

from pydantic import ValidationError

from ..models import DetectedLayout
from .base import SYSTEM_PROMPT, DetectionRequest, ProviderError, ProviderNotConfigured, build_user_prompt, layout_json_schema
from .registry import register

DEFAULT_MODEL = "gemini-3.8-flash"  # current stable (GA) Gemini model


class GeminiLayoutDetector:
    name = "gemini"

    def __init__(self, model: str = DEFAULT_MODEL, client=None):
        from google import genai
        from google.genai import errors, types
        self._types, self._errors = types, errors
        self.model = model
        if client is None:
            try:
                client = genai.Client()
            except Exception as e:  # the SDK raises ValueError when no key is set
                raise ProviderNotConfigured(f"Gemini is not configured: {e}. Set GEMINI_API_KEY.") from e
        self.client = client

    def detect(self, request: DetectionRequest) -> DetectedLayout:
        config = self._types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_json_schema=layout_json_schema(),
        )
        try:
            response = self.client.models.generate_content(
                model=self.model, contents=build_user_prompt(request), config=config)
        except self._errors.APIError as e:
            raise ProviderError(f"Gemini API error {getattr(e, 'code', '')}: {e}") from e
        except Exception as e:  # network failures surface as transport exceptions
            raise ProviderError(f"Gemini request failed: {e}") from e
        text = getattr(response, "text", None)
        if not text:
            raise ProviderError("Gemini returned no content (possibly blocked or empty).")
        try:
            return DetectedLayout.model_validate_json(text)
        except ValidationError as e:
            raise ProviderError(f"Gemini returned JSON that does not match the layout schema: {e}") from e


@register("gemini")
def _make() -> GeminiLayoutDetector:
    try:
        return GeminiLayoutDetector(model=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))
    except ImportError as e:
        raise ProviderNotConfigured("google-genai is not installed (pip install google-genai).") from e
