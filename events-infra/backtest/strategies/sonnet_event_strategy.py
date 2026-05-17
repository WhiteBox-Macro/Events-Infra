"""Sonnet Event Strategy — LLM tags events, algorithm trades from impact table.

Architecture:
  1. LLM ONLY classifies: event text → (category, sub_category, affected_tickers)
  2. Impact table stores: category × ticker → historical return distribution
  3. ALGORITHM decides: lookup category stats → threshold check → trade/no-trade
  4. Direction and sizing are DETERMINISTIC from historical stats, never from LLM opinion

The LLM has no opinion on direction, magnitude, or whether to trade.
It is a tagger, not a decision-maker.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import anthropic

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tick import BarTick, EventTick, Order  # noqa: E402
from engine import StrategyContext  # noqa: E402
from gate_params import GateParams, GLOBAL_DEFAULTS, GateParamsRegistry, BROAD_TICKER  # noqa: E402

log = logging.getLogger("strategy.sonnet_event")

# ── Strategy parameters (deterministic, no LLM involvement) ─────────────
# NOTE: MIN_OBS_TO_TRADE / MIN_HIT_RATE / MIN_AVG_RETURN_BPS are kept here
# as the GLOBAL_DEFAULTS baked into gate_params.GLOBAL_DEFAULTS. Per-(cat,
# ticker) overrides flow through signals.gate_params + GateParamsRegistry.
HOLDING_BARS = 15
POSITION_SIZE_PCT = 0.05        # 5% of portfolio per trade
MAX_CONCURRENT_POSITIONS = 3    # per strategy

# Legacy module-level constants — referenced by older callers; new code should
# use the GateParams dataclass instead. Kept identical to GLOBAL_DEFAULTS so
# behavior is unchanged.
MIN_OBS_TO_TRADE = GLOBAL_DEFAULTS.min_obs
MIN_HIT_RATE = GLOBAL_DEFAULTS.min_hit_rate
MIN_AVG_RETURN_BPS = GLOBAL_DEFAULTS.min_avg_bps

# ── LLM prompt — classification ONLY, no direction/opinion ──────────────
CLASSIFY_PROMPT = """\
You are a financial event tagger. Given a news headline, assign it a category.

Headline: {headline}
Published: {publish_time}
Surprise value: {surprise}

Output a JSON object with ONLY these fields:
{{
  "event_category": string,     // Broad category. Use CONSISTENT labels:
                                // "fed_policy", "earnings_data", "trade_policy", "geopolitical",
                                // "corporate_action", "economic_data", "regulatory", "energy_commodity",
                                // "tech_sector", "labor_market", "fiscal_policy", "other"
  "sub_category": string,       // Specific: "rate_decision", "cpi_release", "tariff_escalation", etc.
  "affected_tickers": string[]  // Which of [{tickers}] are likely relevant. Can be empty.
}}

Rules:
- You are ONLY tagging. Do NOT predict direction, sentiment, or market impact.
- Use CONSISTENT labels so the same type of event always gets the same category.
- Return ONLY the JSON object."""


# ── Impact table: historical event→price reaction stats ─────────────────

@dataclass
class ImpactRecord:
    """Single observation: one event's actual price reaction."""
    category: str
    sub_category: str
    ticker: str
    actual_return: float        # realized return over HOLDING_BARS
    event_time: datetime


class _CatStats:
    """Running statistics for one (category, ticker) pair."""
    __slots__ = ("count", "total", "sq_total", "pos_count")

    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.sq_total = 0.0
        self.pos_count = 0

    def add(self, ret: float) -> None:
        self.count += 1
        self.total += ret
        self.sq_total += ret * ret
        if ret > 0:
            self.pos_count += 1

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count > 0 else 0.0

    @property
    def hit_rate(self) -> float:
        return self.pos_count / self.count if self.count > 0 else 0.5

    @property
    def std(self) -> float:
        if self.count < 2:
            return 0.0
        var = (self.sq_total / self.count) - (self.mean ** 2)
        return var ** 0.5 if var > 0 else 0.0


class ImpactTable:
    """Running statistics of event→price reactions per (category, ticker).

    Returns are TONE-ADJUSTED (symmetric pooling):
    - Bullish event + market up = positive (tone predicted correctly)
    - Bearish event + market down = positive (tone predicted correctly)
    - Bearish event + market up = negative (tone was wrong)

    This pools all tones into one bucket per category while preserving
    directional information. A positive mean means "tone reliably predicts
    direction for this category."
    """

    def __init__(self):
        self._stats: dict[tuple[str, str], _CatStats] = defaultdict(_CatStats)

    def record(self, category: str, tone: str, ticker: str,
               actual_return: float, event_time: datetime) -> None:
        adjusted = self._tone_adjust(actual_return, tone)
        self._stats[(category, ticker)].add(adjusted)

    def lookup(self, category: str, ticker: str) -> Optional[_CatStats]:
        return self._stats.get((category, ticker))

    def lookup_with_fallback(self, category: str, ticker: str,
                              primary_sector: str | None = None
                              ) -> tuple[Optional[_CatStats], str]:
        """Opt-in fallback chain for cold-start categories (B3).

        Tries in order:
          1. (category, ticker)                                -> "specific"
          2. (category, primary_sector)  if sector given       -> "sector"
          3. (category, BROAD_TICKER)                          -> "broad"
          4. nothing                                           -> "surprise_default"

        Returns (stats, fallback_level). The caller decides what to do with
        a "surprise_default" hit (typically: switch side_rule to
        'surprise_direction' regardless of params).

        NOT called by on_event / compute_tilts in this session — flagged
        opt-in per the implementation plan. Callers must wire deliberately.
        """
        s = self._stats.get((category, ticker))
        if s is not None and s.count > 0:
            return s, "specific"
        if primary_sector:
            s = self._stats.get((category, primary_sector))
            if s is not None and s.count > 0:
                return s, "sector"
        s = self._stats.get((category, BROAD_TICKER))
        if s is not None and s.count > 0:
            return s, "broad"
        return None, "surprise_default"

    def summary(self) -> dict:
        out = {}
        for (cat, ticker), s in sorted(self._stats.items(), key=lambda x: -x[1].count):
            out[f"{cat}/{ticker}"] = {
                "n": s.count, "avg_bps": round(s.mean * 10000, 1),
                "hit%": round(s.hit_rate * 100, 1),
            }
        return out

    @staticmethod
    def _tone_adjust(actual_return: float, tone: str) -> float:
        """Sign-adjust return relative to tone.

        Positive result = market moved in tone's expected direction.
        Bearish tone: flip sign (market down = positive).
        Bullish tone: keep sign (market up = positive).
        Neutral/mixed: keep sign (no expectation to adjust for).
        """
        if tone == "bearish":
            return -actual_return
        return actual_return


# ── Decision engine: pure algorithm, no LLM ─────────────────────────────

def decide_trade(stats: Optional[_CatStats], tone: str,
                  params: GateParams = GLOBAL_DEFAULTS,
                  surprise: float | None = None) -> tuple[str | None, float, str]:
    """Deterministic trade decision.

    Dispatches by `params.side_rule`:
      - tone_reliable: impact-table stats + tone (the legacy default path).
                        Also picks up tone_contrarian trades when stats say so.
      - contrarian: strict — only trades when stats meet the contrarian
                     criterion (mean<0, 1-hit_rate>=min_hit_rate).
      - surprise_direction: trade the sign of `surprise` directly. **IGNORES
                             stats, min_obs, min_hit_rate, min_avg_bps, and
                             tone entirely.** Confidence is always 1.0. Use
                             only for scheduled releases where surprise has
                             a well-defined sign (economic_data category).
      - sector_spillover: reserved for B2/B4 work; **loud-fails (returns None
                           with a warning) until properly implemented**.

    `params` defaults to GLOBAL_DEFAULTS so zero-arg call sites keep today's
    behavior unchanged.

    Returns (side, confidence, reason).
    """
    rule = params.side_rule

    # ── surprise_direction: side from surprise sign, no stats / min_obs gating ──
    if rule == "surprise_direction":
        if surprise is None or surprise == 0:
            return None, 0.0, "surprise_direction: no surprise data"
        side = "buy" if surprise > 0 else "sell"
        return side, 1.0, f"surprise_direction: surprise={surprise:+g}"

    # ── sector_spillover not yet wired (B4 future) — loud-fail ──
    if rule == "sector_spillover":
        log.warning("decide_trade: side_rule='sector_spillover' not implemented; "
                    "skipping trade (params.source=%s)", params.source)
        return None, 0.0, "side_rule sector_spillover not yet implemented (B4)"

    # ── tone_reliable / contrarian use impact stats ──
    if stats is None:
        return None, 0.0, "no impact data"

    if stats.count < params.min_obs:
        return None, 0.0, f"insufficient obs ({stats.count}/{params.min_obs})"

    if tone in ("neutral", "mixed"):
        return None, 0.0, "neutral/mixed tone"

    avg_bps = abs(stats.mean * 10000)
    if avg_bps < params.min_avg_bps:
        return None, 0.0, f"avg {avg_bps:.1f}bps below {params.min_avg_bps}bps threshold"

    tone_reliable = stats.mean > 0 and stats.hit_rate >= params.min_hit_rate
    tone_contrarian = stats.mean < 0 and (1 - stats.hit_rate) >= params.min_hit_rate

    if rule == "contrarian":
        if not tone_contrarian:
            return None, 0.0, f"contrarian rule but tone not contrarian (hit={stats.hit_rate:.0%})"
        side = "sell" if tone == "bullish" else "buy"
        confidence = avg_bps / (stats.std * 10000) if stats.std > 0 else 0.0
        return side, confidence, f"contrarian: {stats.count} obs, {avg_bps:.1f}bps avg, {1-stats.hit_rate:.0%} contra-hit"

    # tone_reliable (default path)
    if tone_reliable:
        side = "buy" if tone == "bullish" else "sell"
        confidence = avg_bps / (stats.std * 10000) if stats.std > 0 else 0.0
        return side, confidence, f"tone reliable: {stats.count} obs, {avg_bps:.1f}bps avg, {stats.hit_rate:.0%} hit"
    elif tone_contrarian:
        # legacy behavior: tone_reliable rule still picks contrarian if stats say so
        side = "sell" if tone == "bullish" else "buy"
        confidence = avg_bps / (stats.std * 10000) if stats.std > 0 else 0.0
        return side, confidence, f"contrarian: {stats.count} obs, {avg_bps:.1f}bps avg, {1-stats.hit_rate:.0%} contra-hit"
    else:
        return None, 0.0, f"hit rate {stats.hit_rate:.0%} inconclusive"


# ── Strategy engine ─────────────────────────────────────────────────────

CACHE_PATH = Path(__file__).resolve().parent.parent / "events_classified_cache.json"


class SonnetEventStrategy:
    """LLM tags events (from cache), algorithm trades from historical impact table."""

    name = "sonnet_event"

    def __init__(self, tickers: list[str], model: str = "claude-sonnet-4-6",
                 cache_only: bool = False,
                 gate_registry: GateParamsRegistry | None = None):
        self.tickers = tickers
        self.model = model
        self.cache_only = cache_only
        self._client: anthropic.Anthropic | None = None
        self.impact = ImpactTable()
        self.classification_log: list[dict] = []
        self._last_bar: dict[str, BarTick] = {}
        self._cache: dict[str, dict] = self._load_cache()
        self.last_decisions: list[dict] = []
        self._blacklisted: set[tuple[str, str]] = set()
        # Optional. If None, all lookups return GLOBAL_DEFAULTS — behavior
        # identical to pre-refactor.
        self.gate_registry = gate_registry

        self._pending: list[dict] = []

    def _params_for(self, category: str, ticker: str) -> GateParams:
        if self.gate_registry is None:
            return GLOBAL_DEFAULTS
        return self.gate_registry.lookup(category, ticker)

    def _is_retired(self, category: str, ticker: str) -> bool:
        if self.gate_registry is None:
            return False
        return self.gate_registry.is_retired(category, ticker)

    def reset(self):
        """Reset all mutable state for clean replay (seek to 0%)."""
        self.impact = ImpactTable()
        self.classification_log.clear()
        self._last_bar.clear()
        self.last_decisions.clear()
        self._blacklisted.clear()
        self._pending.clear()

    def _load_cache(self) -> dict:
        if CACHE_PATH.exists():
            try:
                with open(CACHE_PATH, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(
                base_url=os.environ.get("CLASSIFIER_LLM_BASE_URL", "http://192.168.1.10:9210"),
                api_key=os.environ.get("CLASSIFIER_LLM_API_KEY", "event_classifier"),
            )
        return self._client

    def on_bar(self, tick: BarTick, ctx: StrategyContext) -> list[Order]:
        self._last_bar[tick.ticker] = tick

        # Passive observation: check if any pending observations have reached t+15
        remaining = []
        for obs in self._pending:
            if obs.get("recorded"):
                continue
            if obs["ticker"] != tick.ticker:
                remaining.append(obs)
                continue

            obs["bars_elapsed"] = obs.get("bars_elapsed", 0) + 1

            if obs["bars_elapsed"] >= HOLDING_BARS:
                entry_price = obs.get("entry_close")
                if entry_price and entry_price > 0:
                    ret = (tick.close - entry_price) / entry_price
                    self.impact.record(
                        category=obs["category"],
                        tone=obs.get("tone", "neutral"),
                        ticker=obs["ticker"],
                        actual_return=ret,
                        event_time=obs["event_time"],
                    )
                continue
            remaining.append(obs)

        self._pending = remaining
        return []

    def on_event(self, tick: EventTick, ctx: StrategyContext) -> list[Order]:
        self.last_decisions = []
        headline = tick.headline or ""
        if not headline.strip():
            return []

        cached = self._cache.get(str(tick.event_id))
        from_cache = cached is not None
        if cached:
            tag = cached
        elif self.cache_only:
            tag = {
                "event_category": tick.event_type,
                "sub_category": tick.event_type,
                "ticker_impact_weights": {t: 0.5 for t in self.tickers},
            }
            from_cache = False
        else:
            tag = self._classify(headline, tick.publish_time, tick.surprise)
            if tag:
                self._cache[str(tick.event_id)] = tag

        if not tag:
            return []

        category = tag.get("event_category", "other")
        sub_cat = tag.get("sub_category", "")
        weights = tag.get("ticker_impact_weights") or {}
        affected = list(weights.keys()) if weights else (tag.get("affected_tickers") or [])
        sector_impact = tag.get("sector_impact") or []

        self.classification_log.append({
            "event_id": tick.event_id,
            "time": tick.publish_time.isoformat(),
            "headline": headline[:100],
            "category": category,
            "sub_category": sub_cat,
            "affected": affected,
            "weights": weights,
        })

        tone = tick.inferred_tone

        for ticker in affected:
            if ticker in self.tickers:
                last_bar = self._last_bar.get(ticker)
                self._pending.append({
                    "category": category,
                    "sub_category": sub_cat,
                    "tone": tone,
                    "ticker": ticker,
                    "event_time": tick.publish_time,
                    "entry_close": last_bar.close if last_bar else None,
                    "bars_elapsed": 0,
                })

        orders = []
        open_positions = ctx.positions(self.name)
        at_capacity = len(open_positions) >= MAX_CONCURRENT_POSITIONS

        for ticker in affected:
            if ticker not in self.tickers:
                continue

            ticker_weight = float(weights.get(ticker, 0.5))
            classification = {
                "category": category,
                "sub_category": sub_cat,
                "tone": tone,
                "is_regular": tick.is_regular,
                "surprise": float(tick.surprise) if tick.surprise is not None else None,
                "from_cache": from_cache,
                "sector_impact": sector_impact,
            }

            if (category, ticker) in self._blacklisted or self._is_retired(category, ticker):
                retired_reason = ("retired in signals.gate_params"
                                   if self._is_retired(category, ticker)
                                   else "blacklisted at last refit")
                self.last_decisions.append({
                    "event_id": tick.event_id,
                    "headline": headline[:100],
                    "ticker": ticker,
                    "classification": classification,
                    "impact_stats": None,
                    "decision": None,
                    "confidence": 0.0,
                    "reason": retired_reason,
                })
                continue

            stats = self.impact.lookup(category, ticker)
            impact_snap = None
            if stats:
                impact_snap = {
                    "count": stats.count,
                    "avg_bps": round(stats.mean * 10000, 2),
                    "hit_rate": round(stats.hit_rate * 100, 1),
                    "std_bps": round(stats.std * 10000, 2),
                }

            params = self._params_for(category, ticker)
            surprise_val = float(tick.surprise) if tick.surprise is not None else None
            side, confidence, reason = decide_trade(stats, tone, params, surprise_val)

            if at_capacity and side is not None:
                side = None
                reason = f"at max positions ({MAX_CONCURRENT_POSITIONS})"
            elif side is not None and any(p.ticker == ticker for p in open_positions):
                side = None
                reason = f"already positioned in {ticker}"

            self.last_decisions.append({
                "event_id": tick.event_id,
                "headline": headline[:100],
                "ticker": ticker,
                "weight": round(ticker_weight, 2),
                "classification": classification,
                "impact_stats": impact_snap,
                "decision": side,
                "confidence": round(confidence, 4),
                "reason": reason,
            })

            if side is None:
                continue

            scaled_size = POSITION_SIZE_PCT * ticker_weight
            orders.append(Order(
                strategy=self.name,
                ticker=ticker,
                side=side,
                qty_pct=scaled_size,
                reason=f"{category}/{tone}/{sub_cat}",
                submitted_at=tick.publish_time,
                metadata={
                    "event_id": tick.event_id,
                    "category": category,
                    "sub_category": sub_cat,
                    "tone": tone,
                    "impact_n": stats.count if stats else 0,
                    "impact_avg_bps": round(stats.mean * 10000, 2) if stats else 0,
                    "impact_hit_pct": round(stats.hit_rate * 100, 1) if stats else 0,
                    "algo_confidence": round(confidence, 4),
                },
            ))

        return orders

    def compute_tilts(self, tick: EventTick, ctx: StrategyContext) -> dict[str, float]:
        """Compute per-ticker weight tilts for rebalance mode."""
        from portfolio_allocator import compute_tilt as _compute_tilt

        self.last_decisions = []
        headline = tick.headline or ""
        if not headline.strip():
            return {}

        cached = self._cache.get(str(tick.event_id))
        from_cache = cached is not None
        if cached:
            tag = cached
        elif self.cache_only:
            tag = {
                "event_category": tick.event_type,
                "sub_category": tick.event_type,
                "ticker_impact_weights": {t: 0.5 for t in self.tickers},
            }
            from_cache = False
        else:
            tag = self._classify(headline, tick.publish_time, tick.surprise)
            if tag:
                self._cache[str(tick.event_id)] = tag

        if not tag:
            return {}

        category = tag.get("event_category", "other")
        sub_cat = tag.get("sub_category", "")
        weights = tag.get("ticker_impact_weights") or {}
        affected = list(weights.keys()) if weights else (tag.get("affected_tickers") or [])
        sector_impact = tag.get("sector_impact") or []
        tone = tick.inferred_tone

        for ticker in affected:
            if ticker in self.tickers:
                last_bar = self._last_bar.get(ticker)
                self._pending.append({
                    "category": category, "sub_category": sub_cat,
                    "tone": tone, "ticker": ticker,
                    "event_time": tick.publish_time,
                    "entry_close": last_bar.close if last_bar else None,
                    "bars_elapsed": 0,
                })

        tilts = {}
        for ticker in affected:
            if ticker not in self.tickers:
                continue

            ticker_weight = float(weights.get(ticker, 0.5))
            classification = {
                "category": category, "sub_category": sub_cat,
                "tone": tone, "is_regular": tick.is_regular,
                "surprise": float(tick.surprise) if tick.surprise is not None else None,
                "from_cache": from_cache, "sector_impact": sector_impact,
            }

            if (category, ticker) in self._blacklisted or self._is_retired(category, ticker):
                retired_reason = ("retired in signals.gate_params"
                                   if self._is_retired(category, ticker)
                                   else "blacklisted at last refit")
                self.last_decisions.append({
                    "event_id": tick.event_id, "headline": headline[:100],
                    "ticker": ticker, "weight": round(ticker_weight, 2),
                    "classification": classification, "impact_stats": None,
                    "decision": None, "tilt": 0.0,
                    "confidence": 0.0, "reason": retired_reason,
                })
                continue

            stats = self.impact.lookup(category, ticker)
            impact_snap = None
            if stats:
                impact_snap = {
                    "count": stats.count,
                    "avg_bps": round(stats.mean * 10000, 2),
                    "hit_rate": round(stats.hit_rate * 100, 1),
                    "std_bps": round(stats.std * 10000, 2),
                }

            params = self._params_for(category, ticker)
            surprise_val = float(tick.surprise) if tick.surprise is not None else None
            tilt = _compute_tilt(category, ticker, tone, stats, ticker_weight,
                                  params=params, surprise=surprise_val)
            tilts[ticker] = tilt

            direction_label = "overweight" if tilt > 0 else "underweight" if tilt < 0 else None

            self.last_decisions.append({
                "event_id": tick.event_id, "headline": headline[:100],
                "ticker": ticker, "weight": round(ticker_weight, 2),
                "classification": classification, "impact_stats": impact_snap,
                "decision": direction_label, "tilt": round(tilt, 6),
                "confidence": round(abs(tilt) * 1000, 2),
                "reason": f"tilt={tilt:+.4f}" if tilt != 0 else (
                    f"side_rule={params.side_rule} not yet implemented"
                        if params.side_rule == "sector_spillover" else
                    "no impact data" if stats is None else
                    f"insufficient obs ({stats.count}/{params.min_obs})"
                        if stats.count < params.min_obs else
                    "neutral/mixed tone" if tone in ("neutral", "mixed") else
                    "no edge"),
            })

        return tilts

    def record_exit(self, ticker: str, actual_return: float, exit_time: datetime) -> None:
        """Called by runner when position exits. Updates impact table."""
        for pending in self._pending:
            if pending["ticker"] == ticker and pending.get("recorded") is None:
                self.impact.record(
                    category=pending["category"],
                    tone=pending.get("tone", "neutral"),
                    ticker=ticker,
                    actual_return=actual_return,
                    event_time=pending["event_time"],
                )
                pending["recorded"] = True
                break

    def refit(self, train_start: datetime, train_end: datetime, ctx: StrategyContext) -> None:
        log.info("REFIT at %s: %d categories tracked", train_end.strftime("%Y-%m-%d"),
                 len(self.impact._stats))

        self._blacklisted.clear()
        for (cat, ticker), stats in self.impact._stats.items():
            if stats.count >= 10 and stats.hit_rate < 0.45:
                self._blacklisted.add((cat, ticker))
                log.info("  BLACKLIST %s/%s: n=%d hit=%.0f%% avg=%.1fbps",
                         cat, ticker, stats.count, stats.hit_rate * 100, stats.mean * 10000)

        # Refresh agent-proposed / manually-promoted gate params so they take
        # effect at the next event. Safe no-op if registry is None or PG is down.
        if self.gate_registry is not None:
            self.gate_registry.reload()

        summary = self.impact.summary()
        for key, s in list(summary.items())[:10]:
            log.info("  %s: n=%d avg=%.1fbps hit=%.0f%%", key, s["n"], s["avg_bps"], s["hit%"])
        log.info("REFIT done: %d blacklisted", len(self._blacklisted))

    def _classify(self, headline: str, publish_time: datetime, surprise: float | None) -> dict | None:
        prompt = CLASSIFY_PROMPT.format(
            tickers=", ".join(self.tickers),
            headline=headline,
            publish_time=publish_time.strftime("%Y-%m-%d %H:%M UTC"),
            surprise=surprise if surprise is not None else "N/A",
        )
        try:
            resp = self.client.messages.create(
                model=self.model, max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            log.warning("classify failed: %s", e)
            return None

        raw = resp.content[0].text if resp.content else ""
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        try:
            result = json.loads(cleaned)
            if isinstance(result, dict) and "event_category" in result:
                return result
        except json.JSONDecodeError:
            pass
        return None
