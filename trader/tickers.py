"""Three-stage ticker resolver.

Called by the dispatcher on every event. Stages, cheapest first:

  1. Pre-tagged: if the event row already carries `tickers` (Alpha Vantage's
     pre-tagged news, StockTwits cashtags, or any prior enrichment), use
     those. Most production traffic resolves here.

  2. Regex + securities lookup: extract `$AAPL` cashtags and bare uppercase
     1-5 letter tokens from title+summary+body, then validate the candidates
     against AOTC-DB's `stock_os.securities` table. The DB lookup is the
     deny-filter — it turns "GREAT NEWS FROM CEO" into [] cheaply.

  3. LLM fallback: only fired when stages 1-2 yield nothing and the text is
     substantial enough that an LLM read might add value (>=200 chars).
     Gated on Anthropic SDK install + `ANTHROPIC_API_KEY`. In Phase 2 this
     stage is a no-op because the SDK isn't a dep yet; Phase 4 installs it
     and the stage starts working automatically.

The function returns canonicalised, uppercased, deduped symbols.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Iterable

from dbkit import pg

_log = logging.getLogger(__name__)

# Cashtag is the high-confidence signal (e.g. "$AAPL"). Allow lengths 1-5.
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
# Bare uppercase tokens length 1-5. Noisy on its own (matches "CEO", "USA"),
# so the securities lookup is what makes it useful.
_BARE_RE = re.compile(r"\b([A-Z]{1,5})\b")

# Tokens we never want as candidates even if they happen to be tickers. Either
# they're so common in finance news that they swamp signal, or they're English
# words that securities lookup may surprisingly accept (e.g. "ALL" is a ticker).
_STOPWORDS = frozenset({
    "A", "AN", "AS", "AT", "BE", "BY", "CEO", "CFO", "COO", "CTO",
    "EPS", "ETF", "ETFS", "FOR", "FROM", "HE", "I", "IF", "IN", "INC",
    "IPO", "IS", "IT", "ITS", "M&A", "NOT", "NYSE", "OF", "ON", "OR",
    "PR", "Q1", "Q2", "Q3", "Q4", "SEC", "SHE", "SO", "THE", "TO",
    "UK", "US", "USA", "WAS", "WE", "WHO", "WITH", "Y/Y", "YOY",
})

# In-memory securities cache. Refresh every _SECURITIES_TTL seconds.
_SECURITIES_TTL = 600.0
_securities_cache: set[str] | None = None
_securities_cached_at: float = 0.0


def resolve_tickers(
    event: dict,
    *,
    text_fields: Iterable[str] = ("title", "summary", "body"),
    llm_fallback: bool = True,
) -> list[str]:
    """Return canonical tickers mentioned in `event`.

    `event` is a row dict from news.articles, social.posts, or macro.releases.
    For macro releases this normally returns []; the dispatcher decides which
    tickers a macro event affects via downstream rules.
    """
    pretagged = _normalise(event.get("tickers") or [])
    if pretagged:
        return _filter_to_securities(pretagged)

    text = _gather_text(event, text_fields)
    candidates = _extract_candidates(text)
    if candidates:
        filtered = _filter_to_securities(candidates)
        if filtered:
            return filtered

    if llm_fallback and len(text) >= 200:
        try:
            return _llm_fallback(text)
        except Exception:
            _log.debug("llm fallback unavailable; returning empty", exc_info=True)
    return []


# ── Stage 2: regex + securities ──────────────────────────────────────────────
def _gather_text(event: dict, fields: Iterable[str]) -> str:
    parts = []
    for key in fields:
        v = event.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    return " ".join(parts)


def _extract_candidates(text: str) -> list[str]:
    text_upper = text.upper()
    found: set[str] = set()
    for m in _CASHTAG_RE.findall(text_upper):
        if m not in _STOPWORDS:
            found.add(m)
    # Bare uppercase tokens only contribute when there were no cashtags (a
    # text full of cashtags is the higher-signal source). Some texts have a
    # mix — we accept bare candidates either way and let securities lookup
    # filter, but skip if a cashtag form of the same ticker was already found.
    for m in _BARE_RE.findall(text_upper):
        if m in _STOPWORDS or m in found:
            continue
        found.add(m)
    return sorted(found)


def _filter_to_securities(tickers: list[str]) -> list[str]:
    """Drop candidates that aren't in AOTC-DB's stock_os.securities.

    If AOTC-DB isn't reachable we return the candidates unfiltered — the
    downstream dispatcher's watchlist join is the second deny-filter.
    """
    if not tickers:
        return []
    known = _securities_set()
    if not known:
        return tickers
    return [t for t in tickers if t in known]


def _securities_set() -> set[str]:
    global _securities_cache, _securities_cached_at
    now = time.monotonic()
    if _securities_cache is not None and (now - _securities_cached_at) < _SECURITIES_TTL:
        return _securities_cache
    try:
        rows = pg.execute("SELECT symbol FROM stock_os.securities WHERE symbol IS NOT NULL")
    except Exception:
        # AOTC-DB schema absent / table missing — fail open, dispatcher's
        # watchlist filter still catches non-watchlist tickers.
        _log.debug("stock_os.securities unavailable; ticker filter is open", exc_info=True)
        _securities_cache = set()
        _securities_cached_at = now
        return _securities_cache
    _securities_cache = {r["symbol"].upper() for r in rows if r.get("symbol")}
    _securities_cached_at = now
    return _securities_cache


# ── Stage 3: LLM fallback ────────────────────────────────────────────────────
_LLM_PROMPT = (
    "You receive one short piece of financial news. Return ONLY a JSON array "
    "of US-listed ticker symbols (uppercase, no $ prefix) that the article is "
    "directly about. If no specific company is the subject, return [].\n\n"
    "Article:\n{text}\n\nJSON array:"
)


def _llm_fallback(text: str) -> list[str]:
    """Cheap Haiku call to extract tickers from unstructured text.

    Returns [] if the anthropic SDK isn't installed or no key is set. This
    keeps Phase 2 working without the LLM dep; Phase 4 installs anthropic
    and the path starts producing values automatically.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return []
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        return []
    client = anthropic.Anthropic(api_key=api_key)
    # Truncate long bodies — for ticker extraction the first ~1500 chars are
    # plenty and keep latency well under 1s.
    text = text[:1500]
    msg = client.messages.create(
        model=os.environ.get("ANTHROPIC_FAST_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=80,
        messages=[{"role": "user", "content": _LLM_PROMPT.format(text=text)}],
    )
    raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
    return _parse_llm_response(raw)


def _parse_llm_response(raw: str) -> list[str]:
    import json

    raw = raw.strip()
    # Tolerate a code fence the model sometimes wraps around JSON.
    if raw.startswith("```"):
        raw = raw.strip("`").lstrip("json").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    cleaned = _normalise(parsed)
    return _filter_to_securities(cleaned)


# ── Helpers ──────────────────────────────────────────────────────────────────
def _normalise(items: Iterable) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, str):
            continue
        sym = it.strip().lstrip("$").upper()
        if not sym or sym in seen or sym in _STOPWORDS:
            continue
        seen.add(sym)
        out.append(sym)
    return out
