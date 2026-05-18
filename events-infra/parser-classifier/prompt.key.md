# prompt.py

**Purpose:** Unified single-event LLM classifier prompt. One Sonnet 4.6 call per event produces the full structured object (opinion + structural tags + impact weights + scheduled-release block). Replaces the prior Haiku-per-row + Sonnet-batched two-stage pipeline (deleted in this session).

## Key Constants

- `DEFAULT_TARGET_TICKERS` — 15-ticker strategy universe used as the eligible set for `ticker_impacts`.
- `ALLOWED_IMPACT_MARKETS` — 8-value enum: `US_EQUITY`, `US_FI`, `EU_EQUITY`, `EU_FI`, `COMMODITY`, `FX`, `CRYPTO`, `EM`.
- `ALLOWED_ROLES` — `primary`, `sector_spillover`, `broad_market`.
- `MAX_TICKER_IMPACTS = 3` — hard cap; the prompt advises and `classify.py::_clamp_ticker_impacts` enforces.

## Key Functions

- `get_client()` — Anthropic client; reads `CLASSIFIER_LLM_BASE_URL` + `CLASSIFIER_LLM_API_KEY` from env (defaults to Rin proxy).
- `_format_system_prompt(target_tickers)` — fills the prompt template with the universe list.
- `classify_tweet(text, publish_time, target_tickers=None, model="claude-sonnet-4-6")` — main entry. Returns parsed LLM dict or None.
- `reclassify_with_discrepancy(text, publish_time, llm_result, mechanical, discrepancies, target_tickers, model)` — retry call when `extract.find_discrepancies` flags real issues.
- `parse_response(raw)` — strips markdown fences, parses JSON, validates required top-level keys (`event_category`, `event_type`, `tone`, `magnitude`, `is_regular`).

## Output Schema

Single JSON object per call. Top-level keys:
- Identity: `headline`, `text_content`
- Taxonomy: `event_category` (14-label enum), `event_type` (30-label enum), `event_outcome` (sub-classification, nullable), `is_regular`
- Sentiment: `tone`, `magnitude`, `confidence`
- Entities: `primary_ticker` (single, ANY ticker), `ticker_impacts` (list of `{ticker, weight, role}`, max 3, universe-only), `sector` (single nullable string)
- Markets / geo: `impact_markets` (enum array), `countries`
- Scheduled-release block: `indicator_name`, `consensus_value`, `actual_value`, `surprise`, `reporting_period` (all nullable, only when `is_regular`)

## Dependencies

- `anthropic` SDK
- Reads env: `CLASSIFIER_LLM_BASE_URL`, `CLASSIFIER_LLM_API_KEY`

## Gotchas

- The prompt is ADVISORY for `ticker_impacts` constraints — `classify.py::_clamp_ticker_impacts` is the authoritative boundary (drops out-of-universe, caps at 3, validates role enum).
- `target_tickers` parameter flows from `run_classify.py --tickers ...` (default = `DEFAULT_TARGET_TICKERS`). Changing the universe between classifications produces different `ticker_impacts` — re-classify if you change the universe.
- The required-fields validator in `parse_response` is loose (5 keys); the rest of the schema is enforced downstream in `classify.py::build_classified_row`.
