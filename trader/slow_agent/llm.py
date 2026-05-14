"""Two-tier Anthropic LLM config.

Fast model (Haiku) for analyst leaves where there are many cheap calls.
Deep model (Sonnet) for the research manager + portfolio manager where
reasoning quality outweighs per-call cost.

`get_quick_llm()` and `get_deep_llm()` are the only two factories — every
node imports through them so swapping models is a single env change.

We construct lazily; the LangGraph package is part of the optional
`trader` extra, so a slow_agent import path shouldn't crash on a slim
ingester-only install.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

# Lazy imports — the slow agent's runner is the only thing that triggers
# actual LLM construction, so the rest of the package can be imported on a
# trader-extras-not-installed deploy without error.


def _model(name: str, default: str) -> str:
    return os.environ.get(name) or default


@lru_cache(maxsize=2)
def get_quick_llm(*, temperature: float = 0.4) -> Any:
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=_model("ANTHROPIC_FAST_MODEL", "claude-haiku-4-5-20251001"),
        temperature=temperature,
        max_tokens=2048,
    )


@lru_cache(maxsize=2)
def get_deep_llm(*, temperature: float = 0.2) -> Any:
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=_model("ANTHROPIC_DEEP_MODEL", "claude-sonnet-4-6"),
        temperature=temperature,
        max_tokens=4096,
    )


def reset_llm_cache() -> None:
    """Drop cached clients — used in tests after monkeypatching env."""
    get_quick_llm.cache_clear()
    get_deep_llm.cache_clear()


def is_enabled() -> bool:
    """The dispatcher consults this before firing the slow agent — `false`
    cleanly disables Phase 4 without removing the wiring."""
    raw = os.environ.get("SLOW_AGENT_ENABLED", "true").lower()
    if raw in ("false", "0", "no", "off"):
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
