"""LLM prompt template and response parser for event classification."""
from __future__ import annotations

import json
import logging
import os
import re

import anthropic

log = logging.getLogger("classifier.prompt")

SYSTEM_PROMPT = """\
You are a financial event classifier for a trading research system.
Given a raw tweet from a financial news wire (@tradfi), classify it into a structured JSON object.

You must output ALL of these fields:

{
  "headline": string,             // Clean one-line summary. Strip leading * and whitespace.
  "is_regular": boolean,          // TRUE = scheduled periodic release (earnings, economic indicator, quarterly data with actual vs estimate). FALSE = breaking/irregular news.
  "event_type": string,           // MUST use one of: earnings_report, earnings_beat, earnings_miss, guidance_raise, guidance_cut, revenue, deliveries, cpi_release, ppi_release, gdp_release, nfp_release, pmi_release, fomc_decision, rate_hike, rate_cut, rate_hold, tariff_new, tariff_change, sanctions, merger_announced, buyback, restructuring, exec_change, ipo, stock_split, analyst_action, geopolitical, conflict_escalation, diplomacy, policy_statement, market_move, capex, partnership, investigation, legal, product_launch, other
  "tone": "bullish" | "bearish" | "neutral" | "mixed",
  "magnitude": "major" | "moderate" | "minor",
  "impact_markets": string[],     // From: US_EQUITY, US_FI, EU_EQUITY, EU_FI, COMMODITY, FX, CRYPTO, EM
  "tickers": string[],            // All ticker symbols mentioned or implied. Uppercase, no $ prefix.
  "primary_ticker": string | null,
  "sectors": string[],            // e.g. semiconductor, banking, energy, pharma, automotive, tech, defense, retail, media, telecom, ai_infrastructure
  "primary_sector": string | null,
  "countries": string[],          // US, CN, EU, JP, KR, TW, IR, IL, RU, SA, AE, ...
  "confidence": float,            // 0.0-1.0

  // ONLY populate when is_regular=true, otherwise set all to null:
  "indicator_name": string | null,      // CPI, NFP, GDP, PMI, EARNINGS, DELIVERIES, REVENUE, ...
  "consensus_value": number | null,     // The "EST." or expected value
  "actual_value": number | null,        // The actual reported value
  "surprise": number | null,           // actual - consensus
  "reporting_period": string | null     // "2025-Q4", "2026-04", "FY2026", etc.
}

Rules:
- is_regular=true ONLY when the tweet reports a scheduled release with "actual vs estimate" pattern. Examples: "TESLA 4Q DELIVERIES 495,570, EST. 512,277" or "CPI +0.3% M/M, EST +0.2%". A Fed speech or policy statement is NOT regular.
- For is_regular tweets, extract the numeric consensus/actual from the text. surprise = actual - consensus.
- tone: bullish = positive for equity prices. bearish = negative. neutral = factual/informational. mixed = contains both.
- magnitude: major = market-moving (Fed decisions, large earnings beats/misses, geopolitical shocks, tariffs). moderate = notable. minor = routine.
- tickers: extract from cashtags ($NVDA -> NVDA) AND from context (e.g. "TESLA" -> TSLA, "APPLE" -> AAPL). Include ALL mentioned.
- Return ONLY the JSON object. No markdown, no commentary."""


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(
        base_url=os.environ.get("CLASSIFIER_LLM_BASE_URL", "http://192.168.1.10:9210"),
        api_key=os.environ.get("CLASSIFIER_LLM_API_KEY", "event_classifier"),
    )


def classify_tweet(text: str, publish_time: str, model: str = "claude-haiku-4-5-20251001") -> dict | None:
    human_msg = f"Tweet text:\n{text}\n\nPublished: {publish_time}"

    client = get_client()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": human_msg}],
        )
    except Exception as e:
        log.error("LLM call failed: %s", e)
        return None

    raw_text = resp.content[0].text if resp.content else ""
    return parse_response(raw_text)


def reclassify_with_discrepancy(text: str, publish_time: str,
                                 llm_result: dict, mechanical: dict,
                                 discrepancies: list[str],
                                 model: str = "claude-haiku-4-5-20251001") -> dict | None:
    disc_str = "\n".join(f"- {d}" for d in discrepancies)
    human_msg = (
        f"Tweet text:\n{text}\n\nPublished: {publish_time}\n\n"
        f"Your previous classification had discrepancies with mechanical extraction:\n{disc_str}\n\n"
        f"Mechanical extraction found: tickers={mechanical.get('tickers')}, headline={mechanical.get('headline')}\n\n"
        f"Please re-classify, paying attention to the discrepancies. Output the corrected JSON."
    )

    client = get_client()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": human_msg}],
        )
    except Exception as e:
        log.error("reclassify LLM call failed: %s", e)
        return None

    raw_text = resp.content[0].text if resp.content else ""
    return parse_response(raw_text)


def parse_response(raw: str) -> dict | None:
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("failed to parse LLM response as JSON: %s", cleaned[:200])
        return None

    if not isinstance(result, dict):
        log.warning("LLM response is not a dict: %s", type(result))
        return None

    required = ["is_regular", "event_type", "tone", "magnitude"]
    for field in required:
        if field not in result:
            log.warning("missing required field '%s' in LLM response", field)
            return None

    return result
