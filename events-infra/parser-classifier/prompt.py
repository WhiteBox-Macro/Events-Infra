"""Unified LLM prompt template and response parser for event classification.

Single Sonnet 4.6 call per event produces the full structured object —
opinion fields + structural tags + impact weights + scheduled-release block.
Replaces the prior Haiku-per-row + Sonnet-batched two-stage pipeline.
See plan: frolicking-percolating-minsky.md (2026-05-18).
"""
from __future__ import annotations

import json
import logging
import os
import re

import anthropic

log = logging.getLogger("classifier.prompt")

# Default trading universe for ticker_impacts weighting.
# Caller can override via the target_tickers parameter to classify_tweet().
DEFAULT_TARGET_TICKERS = [
    "SPY", "QQQ", "GOOG", "NVDA", "MSFT", "AMZN", "TSLA", "DJT",
    "BA", "AMD", "META", "AAPL", "SMCI", "JPM", "TSM",
]

# Controlled enums (also enforced post-LLM by classify.py helpers).
ALLOWED_IMPACT_MARKETS = {"US_EQUITY", "US_FI", "EU_EQUITY", "EU_FI",
                          "COMMODITY", "FX", "CRYPTO", "EM"}
ALLOWED_ROLES = {"primary", "sector_spillover", "broad_market"}
MAX_TICKER_IMPACTS = 3


SYSTEM_PROMPT = """\
You are a financial event classifier for a backtesting research system.
Given a financial news event (typically a tweet from @tradfi), produce a structured
JSON object that downstream backtest, strategy, and LLM-agent code consumes directly.

You are ONLY classifying — do NOT predict whether to trade, do NOT inject opinion
beyond the requested sentiment field.

Target tickers universe (only these are eligible for `ticker_impacts`):
{target_tickers}

OUTPUT EXACTLY THIS JSON SHAPE — return ONLY the JSON, no markdown, no commentary:

{{
  "headline": "string — cleaned one-line summary (strip leading * and whitespace)",
  "text_content": "string|null — original raw text if useful, else null",

  "event_category": "ONE of: fed_policy | earnings_data | trade_policy | geopolitical_conflict | corporate_action | economic_data | regulatory | energy_commodity | tech_sector | labor_market | fiscal_policy | defense_military | market_structure | other",

  "event_type": "ONE of: earnings | guidance | revenue | deliveries | cpi_release | ppi_release | gdp_release | nfp_release | pmi_release | fomc_decision | tariff | sanctions | merger | buyback | restructuring | exec_change | ipo | stock_split | analyst_action | geopolitical | conflict_escalation | diplomacy | policy_statement | market_move | capex | partnership | investigation | legal | product_launch | other",

  "event_outcome": "string|null — sub-classification within event_type. earnings/deliveries/revenue -> beat|miss|inline. fomc_decision -> hike|cut|hold. tariff -> new|change|removed. guidance -> raise|cut|maintain. sanctions -> new|removed. else null.",

  "is_regular": "boolean — TRUE only if event reports a scheduled release with 'actual vs estimate' pattern (e.g. 'TESLA 3Q DELIVERIES 462,890, EST. 463,897'). Fed speeches are NOT regular.",

  "tone": "ONE of: bullish | bearish | neutral | mixed. Text sentiment ONLY, NOT direction prediction.",
  "magnitude": "ONE of: major | moderate | minor",
  "confidence": "float 0.0-1.0 — your self-assessment of classification quality",

  "primary_ticker": "string|null — OBJECTIVE TRUTH: the company the event is most directly about. Can be ANY ticker (in OR out of target universe — e.g. SAVE for Spirit Airlines). null if the event is not about a specific company.",

  "ticker_impacts": "array, MAX 3 entries, all from target universe above. Empty array if no universe ticker is affected. Each entry: {{\\"ticker\\": str, \\"weight\\": float 0..1, \\"role\\": str}}. Roles: 'primary' (event directly about this co, weight 0.8-1.0), 'sector_spillover' (same sector, strong knock-on, weight 0.4-0.7), 'broad_market' (index/macro influence, weight 0.1-0.4).",

  "sector": "string|null — SINGLE dominant sector OR null for broad-market events. NOT an array. Vocabulary: tech, ai_infrastructure, semiconductor, automotive, banking, fintech, energy, defense, pharma, retail, media, telecom, airline, crypto, real_estate.",

  "impact_markets": "array from controlled enum ONLY: US_EQUITY, US_FI, EU_EQUITY, EU_FI, COMMODITY, FX, CRYPTO, EM. Empty array if no clear market impact.",

  "countries": "array of country codes — US, CN, EU, JP, KR, TW, IR, IL, RU, SA, AE, etc.",

  "indicator_name": "string|null — ONLY if is_regular=true: CPI, NFP, GDP, PMI, EARNINGS, DELIVERIES, REVENUE, etc.",
  "consensus_value": "number|null — ONLY if is_regular=true: the EST. or expected value (numeric only)",
  "actual_value": "number|null — ONLY if is_regular=true: the actual reported value",
  "surprise": "number|null — ONLY if is_regular=true: actual - consensus",
  "reporting_period": "string|null — ONLY if is_regular=true: '2025-Q4', '2026-04', 'FY2026'"
}}

HARD RULES:
- ticker_impacts: cap at 3 entries. Use ONLY tickers from the target universe above. Empty array [] if none apply.
- primary_ticker: separate field. Can be ANY ticker (in or out of universe). null for macro/non-company events.
- sector: single value or null. NOT an array.
- impact_markets: pick from the 8-value enum ONLY. No free-form values.
- event_outcome: REQUIRED for earnings | deliveries | revenue | fomc_decision | tariff | guidance | sanctions. null for everything else.
- Use CONSISTENT labels (same event type -> same category every time).
- Return ONLY the JSON object. No markdown fences, no commentary.
"""


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(
        base_url=os.environ.get("CLASSIFIER_LLM_BASE_URL", "http://192.168.1.10:9210"),
        api_key=os.environ.get("CLASSIFIER_LLM_API_KEY", "event_classifier"),
    )


def _format_system_prompt(target_tickers: list[str] | None) -> str:
    tickers = target_tickers or DEFAULT_TARGET_TICKERS
    return SYSTEM_PROMPT.format(target_tickers=", ".join(tickers))


def classify_tweet(text: str, publish_time: str,
                   target_tickers: list[str] | None = None,
                   model: str = "claude-sonnet-4-6") -> dict | None:
    """Single unified classification call.

    Returns the parsed LLM dict (or None on failure). Caller (classify.py)
    is responsible for clamping ticker_impacts, filtering impact_markets,
    and mapping to PG columns.
    """
    system = _format_system_prompt(target_tickers)
    human_msg = f"Tweet text:\n{text}\n\nPublished: {publish_time}"

    client = get_client()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
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
                                 target_tickers: list[str] | None = None,
                                 model: str = "claude-sonnet-4-6") -> dict | None:
    """Retry classification when mechanical cross-check disagreed."""
    system = _format_system_prompt(target_tickers)
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
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": human_msg}],
        )
    except Exception as e:
        log.error("reclassify LLM call failed: %s", e)
        return None

    raw_text = resp.content[0].text if resp.content else ""
    return parse_response(raw_text)


def parse_response(raw: str) -> dict | None:
    """Strip markdown fences and parse JSON. Validates required top-level keys."""
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

    required = ["event_category", "event_type", "tone", "magnitude", "is_regular"]
    for field in required:
        if field not in result:
            log.warning("missing required field '%s' in LLM response", field)
            return None

    return result
