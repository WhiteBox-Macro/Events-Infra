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
    """Compare LLM output with mechanical extraction. Return list of discrepancy descriptions."""
    issues = []

    mech_tickers = set(mechanical.get("tickers") or [])
    llm_tickers = set(llm_result.get("tickers") or [])

    missing_from_llm = mech_tickers - llm_tickers
    if missing_from_llm:
        issues.append(f"LLM missed tickers found in entities: {sorted(missing_from_llm)}")

    extra_in_llm = llm_tickers - mech_tickers
    if extra_in_llm:
        for t in extra_in_llm:
            if t not in (mechanical.get("headline") or "") and t not in (llm_result.get("headline") or ""):
                issues.append(f"LLM added ticker {t} not found in text or entities — verify")

    return issues
