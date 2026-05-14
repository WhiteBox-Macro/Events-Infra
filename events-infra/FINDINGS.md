# Events-Infra: Exploratory Findings

**Date:** 2026-05-15
**Data:** 3,602 tweets from @tradfi (Oct 2024 – May 2026), classified into `events.classified`
**Price data:** SPY + QQQ 1-min OHLCV bars (same period)
**Convention:** All returns computed vs `open_t0` (pre-event bar open) — no lookahead

---

## 1. Classification Summary

| Metric | Value |
|---|---|
| Total classified | 3,602 |
| Regular (scheduled releases) | 120 |
| Distinct event types | 22 (after consolidation) |
| Reclassified (cross-check) | 85 (2.4%) |
| Classification failures | 0 (after retry) |

### Event type distribution (top 10)
| Type | Count |
|---|---|
| policy_statement | 813 |
| diplomacy | 577 |
| geopolitical | 438 |
| tariff | 252 |
| other | 245 |
| conflict_escalation | 190 |
| market_move | 166 |
| partnership | 145 |
| earnings | 101 |
| legal | 98 |

## 2. Event-Price Alignment

- **SPY:** 3,100 / 3,602 events aligned (86%), 74% within 1 min of a bar
- **QQQ:** 3,239 / 3,602 events aligned (90%), 75% within 1 min of a bar
- **Snap gap median:** -19s (event arrives slightly before bar close — expected)
- ~500 events missed alignment due to falling outside price data range (weekends, gaps)

## 3. Key Observations

### 3.1. `inferred_tone` is NOT a market direction signal

The LLM's tone classification (bullish/bearish/neutral) reflects **text sentiment**, not market impact.

- Bearish tone predicts correct SPY direction only **63% at t0, 58% at t5** (macro events only)
- Bullish tone is a **coin flip** (50%) for SPY direction
- Reason: tone is about the headline, not about what the market has already priced in. "TRUMP: TARIFFS WILL GO UP" is bearish text but SPY rallied +35.7bps because the market expected worse.

**Conclusion:** Tone is metadata for text classification, not a trading feature. The actual market signal depends on expectation vs reality, which tone doesn't capture.

### 3.2. Event type is a more useful organizational dimension

When grouped by `event_type`, the price reactions show more structure than by tone. However, these are **descriptive statistics, not predictive signals** — the market's reaction depends on what was priced in, not just the event category.

#### SPY reactions by event type (in-market events, bps vs open_t0)

| Event Type | n | t0 | t5 | t15 | t60 |
|---|---|---|---|---|---|
| fomc_decision | 60 | -2.2 | -5.7 | -8.0 | -10.5 |
| earnings | 101 | +0.1 | +2.1 | +1.8 | +2.6 |
| conflict_escalation | 190 | -1.7 | -2.0 | -1.4 | -1.7 |
| diplomacy | 577 | +0.3 | +1.7 | +2.9 | +6.4 |
| tariff | 252 | -5.5 | -0.2 | -3.6 | +2.8 |
| sanctions | 17 | -4.2 | -3.2 | -1.9 | -5.8 |

#### QQQ vs SPY — different reactions to the same events

| Event Type | SPY t60 | QQQ t60 | Note |
|---|---|---|---|
| earnings | +2.6 | +13.3 | QQQ 5x more sensitive |
| diplomacy | +6.4 | +8.0 | Both positive, QQQ stronger |
| tariff | +2.8 | +11.4 | **Opposite at t5** — SPY initially down, QQQ up |
| conflict_escalation | -1.7 | -2.1 | Similar |
| fomc_decision | -10.5 | -3.8 | SPY sells harder |

This confirms that market direction is **ticker-dependent** — the same event type produces different (sometimes opposite) reactions across assets. Any predictive model must be trained per-ticker, not with a universal tone label.

### 3.3. Scheduled releases (is_regular=TRUE) show the most structure

| Ticker | n | t5 | t30 | t60 |
|---|---|---|---|---|
| SPY | 116 | +2.1 (std 10.9) | +4.4 (std 17.1) | +4.1 (std 20.0) |
| QQQ | 111 | +4.4 (std 16.6) | +10.3 (std 30.0) | +9.2 (std 34.4) |

Regular events carry `consensus_value`, `actual_value`, and `surprise` — these are the only events where expectation vs reality is directly measurable. Downstream modeling should focus here first.

### 3.4. Taxonomy consolidation

The following event types were merged to remove fragmentation:
- `earnings_report` + `earnings_beat` + `earnings_miss` → `earnings`
- `fomc_decision` + `rate_hold` + `rate_hike` + `rate_cut` → `fomc_decision`
- `tariff_new` + `tariff_change` → `tariff`
- `guidance_raise` + `guidance_cut` → `guidance`
- `merger_announced` → `merger`

Outcome (beat/miss/hold/hike/cut) should be a separate `event_outcome` field, not baked into the type.

## 4. What this does NOT tell us

- **No causal claims.** Price moved after the event ≠ price moved because of the event. Correlation only.
- **No expectations.** Without consensus/pricing data, we can't measure surprise for non-regular events.
- **No volume context.** High-volume moves are different from low-volume drift.
- **Single source.** @tradfi is one news wire. Events may be reported with variable lag vs the actual information release.
- **Index-level only.** SPY/QQQ reactions to single-stock events (e.g., TSLA earnings) are diluted by index composition.

## 5. Infrastructure Built

| Component | Status |
|---|---|
| X ingestion (TPIO + official) | ✓ Complete, SOP locked |
| Datalake (raw JSON on disk) | ✓ 3,602 files |
| events.raw catalog | ✓ 3,602 rows |
| events.classified (LLM + cross-check) | ✓ 3,602 rows, 0 failures |
| Price alignment script | ✓ Lookahead-safe convention |
| SPY/QQQ 1-min parquet | ✓ Oct 2024 – May 2026 |
