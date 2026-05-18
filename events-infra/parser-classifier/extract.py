"""Mechanical field extraction for cross-checking LLM output."""
from __future__ import annotations

import re
from datetime import datetime


def extract_mechanical(payload: dict) -> dict:
    """Extract fields mechanically from raw TPIO tweet payload.

    Returns dict with: headline, tickers, primary_ticker, publish_time.
    Used to cross-check LLM classification.
    """
    text = payload.get("text") or ""

    headline = text.split("\n")[0].strip().lstrip("*").strip()

    symbols = payload.get("entities", {}).get("symbols") or []
    tickers_from_entities = [s["text"].upper() for s in symbols if s.get("text")]

    cashtag_pattern = re.compile(r"\$([A-Z]{1,5})\b")
    tickers_from_regex = cashtag_pattern.findall(text)

    all_tickers = list(dict.fromkeys(tickers_from_entities + tickers_from_regex))

    created_at = payload.get("createdAt", "")
    try:
        publish_time = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, TypeError):
        publish_time = None

    return {
        "headline": headline,
        "tickers": all_tickers,
        "primary_ticker": all_tickers[0] if all_tickers else None,
        "publish_time": publish_time,
    }


def find_discrepancies(llm_result: dict, mechanical: dict) -> list[str]:
    """Compare LLM output with mechanical extraction.

    Post-unified-prompt (2026-05-18): the LLM emits `primary_ticker` (objective
    truth, any ticker) + `ticker_impacts[].ticker` (universe-constrained).
    Mechanical extraction pulls all cashtags from the raw text.

    Only flag GENUINE issues:
      1. primary_ticker is set but doesn't appear in cashtags or headline
         (likely LLM hallucination).
      2. A mechanically-extracted ticker IS in the target universe but missing
         from BOTH primary_ticker and ticker_impacts (LLM missed a relevant
         signal). We can't tell universe membership here, so defer: only flag
         if primary_ticker is empty AND mech tickers exist (the LLM ignored
         what was clearly a company-specific event).
    """
    issues = []

    mech_tickers = set(mechanical.get("tickers") or [])
    primary = llm_result.get("primary_ticker")
    impacts = llm_result.get("ticker_impacts") or []
    impact_tickers = {e.get("ticker") for e in impacts
                       if isinstance(e, dict) and e.get("ticker")}
    llm_tickers = ({primary} if primary else set()) | impact_tickers

    headline = (llm_result.get("headline") or "") + " " + (mechanical.get("headline") or "")

    # Hallucination: primary_ticker not in cashtags AND not in headline text
    if primary and mech_tickers and primary not in mech_tickers and primary not in headline:
        issues.append(f"LLM primary_ticker={primary} not in cashtags {sorted(mech_tickers)} or headline")

    # Ignored company event: mech extracted cashtags but LLM emitted nothing
    if mech_tickers and not llm_tickers:
        issues.append(f"LLM emitted no tickers despite cashtags {sorted(mech_tickers)} in text")

    return issues
