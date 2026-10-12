"""Provider registry. Select with LLM_PROVIDER (default: gemini).

Adding a provider:
    @register("openai")
    def _make() -> LayoutDetector: return OpenAILayoutDetector(...)
and import the module in providers/__init__.py.
"""
from __future__ import annotations

import os
from collections.abc import Callable

from .base import LayoutDetector, ProviderNotConfigured

DEFAULT_PROVIDER = "gemini"
_FACTORIES: dict[str, Callable[[], LayoutDetector]] = {}


def register(name: str):
    def wrap(factory: Callable[[], LayoutDetector]):
        _FACTORIES[name] = factory
        return factory
    return wrap


def available() -> list[str]:
    return sorted(_FACTORIES)


def get_detector(name: str | None = None) -> LayoutDetector:
    name = (name or os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).lower()
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ProviderNotConfigured(f"Unknown LLM provider '{name}'. Available: {', '.join(available())}.")
    return factory()
