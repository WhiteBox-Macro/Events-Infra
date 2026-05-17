# gate_params.py

**Purpose:** In-memory cache of `signals.gate_params` rows, with fail-safe fallback to module-level `GLOBAL_DEFAULTS`. Replaces the hand-tuned constants in `sonnet_event_strategy.py` with a writable surface that agents can tune per-(event_category, ticker).

## Key Types

- **`GateParams`** (frozen dataclass) — `min_obs`, `min_hit_rate`, `min_avg_bps`, `holding_bars`, `side_rule`, `tilt_unit`, `source`
- **`GLOBAL_DEFAULTS`** — `GateParams()` with today's tuning (3 obs / 0.55 hit / 2 bps / 15 bar / tone_reliable / 0.01 tilt)
- **`GateParamsRegistry`** — dict-backed cache, populated from PG via `reload()`
- **`SIDE_RULES`** — tuple of valid `side_rule` values: `tone_reliable | contrarian | surprise_direction | sector_spillover`
- **`BROAD_TICKER`** = `"BROAD"` — sentinel for category-level fallback rows

## Registry Methods

- `reload()` — TWO queries: (1) `WHERE status='active'` via `DISTINCT ON (cat, ticker) ORDER BY version DESC` populates `_by_key` with highest-version active params; (2) `WHERE status='retired'` returns distinct (cat, ticker) keys, **filtered to those NOT already in `_by_key`** before adding to `_retired`. Two-query design prevents the retired-shadow trap where a higher-version retired row would hide a still-valid lower-version active row. Returns active count. Survives missing table / dead PG (logs warning, returns 0).
- `lookup(category, ticker)` — chain: `(cat, ticker)` → `(cat, BROAD)` (marks `source='fallback:broad'`) → `GLOBAL_DEFAULTS`. Never raises.
- `is_retired(category, ticker)` — O(1) check against in-memory `_retired` set. No PG round-trip in the hot path.
- `__len__()` — count of active rows

## Module Helper

- `default_registry()` — lazy module-level singleton. Most callers should use this; subagents / tests can build their own.

## Lookup Semantics

```
specific row (status='active')           -> use it
no specific row, BROAD row exists        -> use BROAD with source='fallback:broad'
neither                                  -> GLOBAL_DEFAULTS (source='default')
```

`status='retired'` rows are NOT loaded into the cache — strategy must call `is_retired()` separately to enforce the blacklist.

## Dependencies

- `dbkit.pg` (optional — registry survives its absence)

## Gotchas

- Frozen `GateParams` instances are safe to share across threads. Mutation requires constructing a new instance.
- Empty `signals.gate_params` table is normal at startup. Strategy behaves identically to pre-refactor in that case.
- `reload()` should be called at walk-forward refit so newly-promoted gates take effect.
- `is_retired()` is in-memory only; calling it has no PG cost. Retired keys refresh at the next `reload()`.
