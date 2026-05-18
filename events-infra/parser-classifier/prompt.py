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
You are a sell-side equity analyst / trader classifying financial news for
a backtesting research system. Read every event through one lens:

  HOW DOES THIS EVENT AFFECT THE AFFECTED COMPANY'S (OR INDEX'S)
  FUTURE REVENUE-GENERATION ABILITY?

That is the central question. Strike continues → harder to ship product →
bearish. Beat earnings + raise guidance → demand strong → bullish. Tariff
on imports a company depends on → margins pressured → bearish. Plant
reopens after outage → bullish reversal.

Produce a structured JSON object that downstream backtest, strategy, and
LLM-agent code consumes directly. Do NOT predict trade size or timing —
only encode the *directional implication for future revenues* in `tone`.

Target tickers universe (only these are eligible for `ticker_impacts`):
{target_tickers}

DISAMBIGUATION RULES (read first):

1. Company vs topic. If the event is fundamentally about a SPECIFIC
   COMPANY (you can set `primary_ticker`), the category is
   `corporate_action` EVEN IF the surface topic is labor, legal,
   regulatory, product safety, etc. Use thematic categories
   (`labor_market`, `regulatory`, `fed_policy`) only when the event is
   MACRO/POLICY with no specific company anchor.
     - "Boeing workers reject deal" → corporate_action + labor_action
     - "U.S. nonfarm payrolls +254k vs est +150k" → labor_market + nfp_release

2. Operational disruptions are BEARISH for the affected company unless
   they resolve. Strike continues/extends/rejected, plant fire, outage,
   recall, product safety event → bearish. Strike resolves, plant
   reopens, deal accepted, recall withdrawn → bullish.

3. Earnings/data surprise direction usually drives tone:
   actual > consensus → bullish. actual < consensus → bearish.
   Neutral only when the magnitude is trivially small relative to noise.

4. Policy events:
     - tariff new on imports → bearish for importers; bullish for domestic
       producers of substitutes
     - sanctions new → bearish for sanctioned-country exporters
     - FOMC cut → generally bullish for risk assets; hike → bearish
     - fiscal stimulus → bullish; austerity → bearish

5. Magnitude anchor (a stock-move proxy, not a target):
     - major  → would move underlying >5% or shifts narrative for the year
     - moderate → would move underlying 1–5% or shifts narrative for the quarter
     - minor  → <1% / noise

OUTPUT EXACTLY THIS JSON SHAPE — return ONLY the JSON, no markdown, no commentary:

{{
  "headline": "string — cleaned one-line summary (strip leading * and whitespace)",
  "text_content": "string|null — original raw text if useful, else null",

  "event_category": "ONE of: fed_policy | earnings_data | trade_policy | geopolitical_conflict | corporate_action | economic_data | regulatory | energy_commodity | tech_sector | labor_market | fiscal_policy | defense_military | market_structure | other",

  "event_type": "ONE of: earnings | guidance | revenue | deliveries | cpi_release | ppi_release | gdp_release | nfp_release | pmi_release | fomc_decision | tariff | sanctions | merger | buyback | restructuring | exec_change | ipo | stock_split | analyst_action | geopolitical | conflict_escalation | diplomacy | policy_statement | market_move | capex | partnership | investigation | legal | product_launch | labor_action | recall | operational_disruption | other",

  "event_outcome": "string|null — sub-classification within event_type. earnings/deliveries/revenue -> beat|miss|inline. fomc_decision -> hike|cut|hold. tariff -> new|change|removed. guidance -> raise|cut|maintain. sanctions -> new|removed. labor_action -> start|extend|reject|resolve. recall -> issue|expand|resolve. operational_disruption -> start|extend|resolve. else null.",

  "is_regular": "boolean — TRUE only if event reports a scheduled release with 'actual vs estimate' pattern (e.g. 'TESLA 3Q DELIVERIES 462,890, EST. 463,897'). Fed speeches are NOT regular.",

  "tone": "ONE of: bullish | bearish | neutral | mixed. THE DIRECTIONAL READ on future revenue generation for the affected ticker(s). Not literal sentiment — judgment.",
  "magnitude": "ONE of: major | moderate | minor (see anchor above)",
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
- event_outcome: REQUIRED for earnings | deliveries | revenue | fomc_decision | tariff | guidance | sanctions | labor_action | recall | operational_disruption. null for everything else.
- Use CONSISTENT labels (same event type -> same category every time).
- Return ONLY the JSON object. No markdown fences, no commentary.

WORKED EXAMPLES (study the reasoning; emit only the final JSON for the real event):

— Example 1 —
Tweet: "Boeing workers reject tentative labor deal, union says"
Reasoning: Company-specific labor event → corporate_action (NOT labor_market).
Strike continues → BA production halted longer → future revenue impaired → bearish.
Type=labor_action, outcome=reject. BA is in universe.
JSON: {{"event_category":"corporate_action","event_type":"labor_action","event_outcome":"reject","is_regular":false,"tone":"bearish","magnitude":"major","confidence":0.9,"primary_ticker":"BA","ticker_impacts":[{{"ticker":"BA","weight":1.0,"role":"primary"}}],"sector":"defense","impact_markets":["US_EQUITY"],"countries":["US"],"indicator_name":null,"consensus_value":null,"actual_value":null,"surprise":null,"reporting_period":null,"headline":"Boeing workers reject tentative labor deal","text_content":null}}

— Example 2 —
Tweet: "TESLA 3Q DELIVERIES 462,890, EST. 463,897 $TSLA"
Reasoning: Scheduled deliveries print, actual < est by 1007 → miss (tiny).
Below-consensus deliveries → modestly bearish for TSLA revenue print.
JSON: {{"event_category":"corporate_action","event_type":"deliveries","event_outcome":"miss","is_regular":true,"tone":"bearish","magnitude":"moderate","confidence":0.95,"primary_ticker":"TSLA","ticker_impacts":[{{"ticker":"TSLA","weight":0.95,"role":"primary"}},{{"ticker":"SPY","weight":0.15,"role":"broad_market"}},{{"ticker":"QQQ","weight":0.15,"role":"broad_market"}}],"sector":"automotive","impact_markets":["US_EQUITY"],"countries":["US"],"indicator_name":"DELIVERIES","consensus_value":463897,"actual_value":462890,"surprise":-1007,"reporting_period":"2024-Q3","headline":"Tesla 3Q deliveries 462,890 vs est 463,897","text_content":null}}

— Example 3 —
Tweet: "FED LEAVES RATES UNCHANGED"
Reasoning: Macro policy, no company anchor → fed_policy. Rate hold = neutral baseline.
JSON: {{"event_category":"fed_policy","event_type":"fomc_decision","event_outcome":"hold","is_regular":false,"tone":"neutral","magnitude":"major","confidence":0.95,"primary_ticker":null,"ticker_impacts":[],"sector":null,"impact_markets":["US_EQUITY","US_FI","FX"],"countries":["US"],"indicator_name":null,"consensus_value":null,"actual_value":null,"surprise":null,"reporting_period":null,"headline":"Fed leaves rates unchanged","text_content":null}}

— Example 4 —
Tweet: "TRUMP: WE ARE THINKING IN TERMS OF 25% TARIFFS ON MEXICO"
Reasoning: Trade policy. Mexico tariffs hurt importers (autos, retail, ag) → bearish broad US equity. DJT is "primary" as objective truth of who said it.
JSON: {{"event_category":"trade_policy","event_type":"tariff","event_outcome":"new","is_regular":false,"tone":"bearish","magnitude":"major","confidence":0.85,"primary_ticker":"DJT","ticker_impacts":[{{"ticker":"DJT","weight":0.9,"role":"primary"}},{{"ticker":"SPY","weight":0.35,"role":"broad_market"}},{{"ticker":"QQQ","weight":0.3,"role":"broad_market"}}],"sector":null,"impact_markets":["US_EQUITY","FX","COMMODITY"],"countries":["US","MX"],"indicator_name":null,"consensus_value":null,"actual_value":null,"surprise":null,"reporting_period":null,"headline":"Trump considers 25% tariffs on Mexico","text_content":null}}

— Example 5 —
Tweet: "CDC: Severe E. coli outbreak linked to McDonald's"
Reasoning: Company-specific food safety → corporate_action + recall. Customers avoid MCD short-term → revenue impaired → bearish. MCD not in target universe.
JSON: {{"event_category":"corporate_action","event_type":"recall","event_outcome":"issue","is_regular":false,"tone":"bearish","magnitude":"moderate","confidence":0.9,"primary_ticker":"MCD","ticker_impacts":[],"sector":"retail","impact_markets":["US_EQUITY"],"countries":["US"],"indicator_name":null,"consensus_value":null,"actual_value":null,"surprise":null,"reporting_period":null,"headline":"CDC: severe E. coli outbreak linked to McDonald's","text_content":null}}

— Example 6 —
Tweet: "U.S. OCT NONFARM PAYROLLS +254K, EST +150K"
Reasoning: Macro labor data print (no specific company) → labor_market. Hot labor = consumer spending stays strong → bullish broad equity (but raises Fed-hawkish risk; net bullish).
JSON: {{"event_category":"labor_market","event_type":"nfp_release","event_outcome":null,"is_regular":true,"tone":"bullish","magnitude":"major","confidence":0.85,"primary_ticker":null,"ticker_impacts":[{{"ticker":"SPY","weight":0.3,"role":"broad_market"}},{{"ticker":"QQQ","weight":0.25,"role":"broad_market"}}],"sector":null,"impact_markets":["US_EQUITY","US_FI"],"countries":["US"],"indicator_name":"NFP","consensus_value":150000,"actual_value":254000,"surprise":104000,"reporting_period":"2024-10","headline":"US Oct nonfarm payrolls +254k vs est +150k","text_content":null}}

— Example 7 —
Tweet: "NVIDIA partners with TSMC for next-gen 2nm chip production"
Reasoning: Company-specific partnership benefiting NVDA (locks in supply) and TSM (revenue commitment). Both bullish; AMD as semiconductor sector spillover (mild).
JSON: {{"event_category":"corporate_action","event_type":"partnership","event_outcome":null,"is_regular":false,"tone":"bullish","magnitude":"moderate","confidence":0.85,"primary_ticker":"NVDA","ticker_impacts":[{{"ticker":"NVDA","weight":0.9,"role":"primary"}},{{"ticker":"TSM","weight":0.8,"role":"primary"}},{{"ticker":"AMD","weight":0.3,"role":"sector_spillover"}}],"sector":"semiconductor","impact_markets":["US_EQUITY"],"countries":["US","TW"],"indicator_name":null,"consensus_value":null,"actual_value":null,"surprise":null,"reporting_period":null,"headline":"NVIDIA partners with TSMC for 2nm chip production","text_content":null}}

— Example 8 —
Tweet: "Account introduction post for market-moving stock news account"
Reasoning: Meta/operational, no market signal.
JSON: {{"event_category":"other","event_type":"other","event_outcome":null,"is_regular":false,"tone":"neutral","magnitude":"minor","confidence":0.95,"primary_ticker":null,"ticker_impacts":[],"sector":null,"impact_markets":[],"countries":[],"indicator_name":null,"consensus_value":null,"actual_value":null,"surprise":null,"reporting_period":null,"headline":"Account introduction","text_content":null}}
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
