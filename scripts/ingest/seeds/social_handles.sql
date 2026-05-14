-- Seed rows for social.handles. Edit before applying:
--   psql $DATABASE_URL -f scripts/ingest/seeds/social_handles.sql
--
-- impact_weight is a curator-set multiplier applied to the fast-signal
-- score on top of the follower-tier multiplier. Defaults:
--   3.0  — highest-impact (head-of-state, sitting central banker)
--   2.0  — major market mover (Elon-scale CEO, top-tier wire reporter)
--   1.5  — strong but narrower (sector CEO, specific-beat reporter)
--   1.0  — broad-coverage account, no special weighting
--
-- tags / expected_themes feed the LLM theme-to-ticker inference prompt so
-- it knows what a given handle usually talks about; edit to taste.
--
-- VERIFY HANDLES BEFORE INSERTING — accounts get suspended, renamed, or
-- migrated; nothing in this seed is checked at apply time.

INSERT INTO social.handles
  (platform, username, display_name, category, tags, expected_themes, impact_weight, poll_interval_sec, metadata)
VALUES

-- ── Heads of state / politicians ────────────────────────────────────────
('twitter', 'realDonaldTrump', 'Donald J. Trump', 'politician',
 ARRAY['us_politics','geopolitics'],
 ARRAY['tariffs','china','energy','defense','immigration'],
 3.0, 30,
 '{"why": "head of state — moves entire sectors with single posts"}'::jsonb),

('twitter', 'POTUS', 'POTUS (institutional)', 'politician',
 ARRAY['us_politics'],
 ARRAY['executive_orders','foreign_policy'],
 2.5, 30,
 '{"why": "official US presidency account"}'::jsonb),

-- ── CEOs ───────────────────────────────────────────────────────────────
('twitter', 'elonmusk', 'Elon Musk', 'ceo',
 ARRAY['tech','ev','crypto','ai'],
 ARRAY['tesla','spacex','x_platform','ai','autonomy','crypto_sentiment'],
 2.5, 30,
 '{"why": "single tweets move TSLA, DOGE, broader EV / AI complex"}'::jsonb),

-- ── Central bank / fiscal authorities ──────────────────────────────────
('twitter', 'federalreserve', 'Federal Reserve', 'agency',
 ARRAY['monetary_policy','macro'],
 ARRAY['rates','balance_sheet','fomc'],
 3.0, 60,
 '{"why": "official Fed press"}'::jsonb),

('twitter', 'USTreasury', 'US Treasury', 'agency',
 ARRAY['fiscal_policy','macro'],
 ARRAY['debt_issuance','sanctions','treasury_market'],
 2.0, 60,
 '{"why": "official US Treasury"}'::jsonb),

-- ── Wire-style breaking-news accounts ──────────────────────────────────
('twitter', 'DeItaone', 'Walter Bloomberg', 'journalist',
 ARRAY['wire','breaking'],
 ARRAY['earnings_pre_market','m_and_a','macro_prints'],
 2.0, 30,
 '{"why": "fast wire-headline forwarder; routinely first on breaking finance items"}'::jsonb),

('twitter', 'FirstSquawk', 'First Squawk', 'journalist',
 ARRAY['wire','breaking','geopolitics'],
 ARRAY['middle_east','energy','rates'],
 2.0, 30,
 '{"why": "fast wire feed, heavier on geopolitics than DeItaone"}'::jsonb),

('twitter', 'unusual_whales', 'Unusual Whales', 'analyst',
 ARRAY['options_flow','retail','political_trades'],
 ARRAY['unusual_options','congress_trades'],
 1.5, 60,
 '{"why": "options-flow + congressional-trade alerts; useful for single-name color"}'::jsonb),

('twitter', 'zerohedge', 'ZeroHedge', 'macro_pundit',
 ARRAY['macro','contrarian'],
 ARRAY['rates','geopolitics','china'],
 1.0, 60,
 '{"why": "high-volume macro takes; weight kept low because signal-to-noise is variable"}'::jsonb)

ON CONFLICT (platform, username) DO NOTHING;
