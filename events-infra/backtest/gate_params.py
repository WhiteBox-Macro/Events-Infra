"""Decision-gate parameters loaded from signals.gate_params.

Replaces the module-level constants in sonnet_event_strategy.py with a
writable surface that agents can tune per-(event_category, ticker).

Lookup chain:
    (category, ticker) -> (category, "BROAD") -> GLOBAL_DEFAULTS

GLOBAL_DEFAULTS preserves today's behavior (3 obs, 0.55 hit, 2 bps avg,
15-bar hold, tone_reliable). An empty signals.gate_params table is fine --
strategy falls through to defaults for every lookup.

Cold path: PG SELECT during reload(). Hot path: dict lookup.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("gate_params")

SIDE_RULES = ("tone_reliable", "contrarian", "surprise_direction", "sector_spillover")
BROAD_TICKER = "BROAD"


@dataclass(frozen=True)
class GateParams:
    """Per-(category, ticker) gate parameters. Frozen so registry entries
    are safe to share across threads."""

    min_obs: int = 3
    min_hit_rate: float = 0.55
    min_avg_bps: float = 2.0
    holding_bars: int = 15
    side_rule: str = "tone_reliable"
    tilt_unit: float = 0.01
    source: str = "default"   # 'default' | 'db:<gate_id>' | 'fallback:broad'


GLOBAL_DEFAULTS = GateParams()


class GateParamsRegistry:
    """In-memory cache of signals.gate_params.

    Populated by reload() at startup and after each walk-forward refit.
    Lookup is fail-safe: empty table => every lookup returns GLOBAL_DEFAULTS.
    """

    def __init__(self):
        self._by_key: dict[tuple[str, str], GateParams] = {}
        self._retired: set[tuple[str, str]] = set()

    def reload(self) -> int:
        """Load active rows (highest-version per key) into the params dict.
        Load retired keys (those with NO active row) into the retired set.

        Two-query design intentionally avoids the DISTINCT-ON-shadow trap
        where a higher-version retired row hides a still-valid lower-version
        active row. Semantics: a key is retired only if no active version
        exists for it.

        Returns active row count. Safe when the table is missing or PG is
        down (logs warning, leaves registry empty)."""
        self._by_key.clear()
        self._retired.clear()
        try:
            from dbkit import pg
        except ImportError:
            log.warning("dbkit.pg unavailable; registry stays empty")
            return 0

        try:
            active_rows = pg.execute(
                """
                SELECT DISTINCT ON (event_category, ticker)
                    gate_id, event_category, ticker,
                    min_obs, min_hit_rate, min_avg_bps,
                    holding_bars, side_rule, tilt_unit
                FROM signals.gate_params
                WHERE status = 'active'
                ORDER BY event_category, ticker, version DESC
                """
            )
            retired_rows = pg.execute(
                """
                SELECT DISTINCT event_category, ticker
                FROM signals.gate_params
                WHERE status = 'retired'
                """
            )
        except Exception as e:
            # Table might not exist yet (pre-migration) or PG might be down.
            log.warning("gate_params reload failed (%s); registry stays empty", e)
            return 0

        for r in active_rows:
            key = (r["event_category"], r["ticker"])
            self._by_key[key] = GateParams(
                min_obs=int(r["min_obs"]),
                min_hit_rate=float(r["min_hit_rate"]),
                min_avg_bps=float(r["min_avg_bps"]),
                holding_bars=int(r["holding_bars"]),
                side_rule=r["side_rule"],
                tilt_unit=float(r["tilt_unit"]),
                source=f"db:{r['gate_id']}",
            )

        # A key is retired only if it has NO active version. This lets users
        # retire a high version and fall back to a lower active version.
        for r in retired_rows:
            key = (r["event_category"], r["ticker"])
            if key not in self._by_key:
                self._retired.add(key)

        log.info("gate_params loaded: %d active, %d retired", len(self._by_key), len(self._retired))
        return len(self._by_key)

    def lookup(self, category: str, ticker: str) -> GateParams:
        """Try (cat, ticker) -> (cat, BROAD) -> GLOBAL_DEFAULTS."""
        specific = self._by_key.get((category, ticker))
        if specific is not None:
            return specific
        broad = self._by_key.get((category, BROAD_TICKER))
        if broad is not None:
            # Mark as fallback so callers can log/dashboard differently
            return GateParams(
                min_obs=broad.min_obs,
                min_hit_rate=broad.min_hit_rate,
                min_avg_bps=broad.min_avg_bps,
                holding_bars=broad.holding_bars,
                side_rule=broad.side_rule,
                tilt_unit=broad.tilt_unit,
                source=f"fallback:broad ({broad.source})",
            )
        return GLOBAL_DEFAULTS

    def is_retired(self, category: str, ticker: str) -> bool:
        """O(1) check against the in-memory retired set populated by reload().
        Use to blacklist gates that an agent (or manual review) marked retired."""
        return (category, ticker) in self._retired

    def __len__(self) -> int:
        return len(self._by_key)


_DEFAULT_REGISTRY: Optional[GateParamsRegistry] = None


def default_registry() -> GateParamsRegistry:
    """Lazy module-level registry for callers that don't want to manage one.
    Reloaded once on first access; callers wanting refreshed data should
    call .reload() explicitly (e.g. at walk-forward refit time)."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = GateParamsRegistry()
        _DEFAULT_REGISTRY.reload()
    return _DEFAULT_REGISTRY
