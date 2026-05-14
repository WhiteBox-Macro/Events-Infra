-- Seed rows for news.sources.
--
-- One-shot: run with `psql $DATABASE_URL -f scripts/ingest/seeds/news_sources.sql`.
-- ON CONFLICT (name) DO NOTHING — safe to re-run.
--
-- Priorities reflect the latency comparison: SEC EDGAR + Federal Reserve +
-- Treasury are tier-1 free real-time wires (original source, no aggregator
-- latency). MarketWatch is fast-ish secondary. alpha_vantage_news is the
-- aggregator path; we keep it as a source row so the ingester has an id.
--
-- IMPORTANT: SEC requires a contact email in the User-Agent header on every
-- request, or it serves 403. The metadata JSONB carries a per-source
-- user_agent override that news_rss.py applies. EDIT THE EMAIL BELOW.

INSERT INTO news.sources (name, publisher, feed_type, feed_url, category, poll_interval_sec, metadata) VALUES

-- ── SEC EDGAR (regulatory; 8-Ks move stocks immediately) ────────────────
('sec_edgar_8k',
 'SEC EDGAR',
 'atom',
 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom',
 'regulatory',
 30,
 '{"user_agent": "AOTC-Signals contact@example.com", "priority": 5}'::jsonb),

('sec_edgar_all_current',
 'SEC EDGAR',
 'atom',
 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&output=atom',
 'regulatory',
 60,
 '{"user_agent": "AOTC-Signals contact@example.com", "priority": 3}'::jsonb),

-- ── Federal Reserve (macro; rate decisions move every market) ───────────
('fed_press_all',
 'Federal Reserve',
 'rss',
 'https://www.federalreserve.gov/feeds/press_all.xml',
 'macro',
 60,
 '{"priority": 4}'::jsonb),

('fed_press_monetary',
 'Federal Reserve',
 'rss',
 'https://www.federalreserve.gov/feeds/press_monetary.xml',
 'macro',
 30,
 '{"priority": 5}'::jsonb),

-- ── US Treasury (macro / fiscal) ────────────────────────────────────────
('treasury_press',
 'US Treasury',
 'rss',
 'https://home.treasury.gov/news/press-releases/feed',
 'macro',
 120,
 '{"priority": 3}'::jsonb),

-- ── MarketWatch top stories (general financial news, decent latency) ────
('marketwatch_topstories',
 'MarketWatch',
 'rss',
 'https://feeds.content.dowjones.io/public/rss/mw_topstories',
 'business',
 60,
 '{"priority": 2}'::jsonb),

-- ── Alpha Vantage aggregator (api; news_alpha_vantage.py uses this id) ──
('alpha_vantage_news',
 'Alpha Vantage Aggregated',
 'api',
 'alphavantage:NEWS_SENTIMENT',
 'business',
 60,
 '{"priority": 2, "via": "alpha_vantage"}'::jsonb)

ON CONFLICT (name) DO NOTHING;

-- ── Commented additions to consider as you upgrade ──────────────────────
-- After you sign up, replace the user_agent and uncomment as needed:
--   ('reuters_business',  'Reuters',          'rss',  '<reuters access URL>',         'business',   30,  '{"priority": 5}'::jsonb)
--   ('benzinga_pro_ws',   'Benzinga Pro',     'api',  'wss://api.benzinga.com/...',   'business',   1,   '{"priority": 5, "requires_paid_key": true}'::jsonb)
--   ('polygon_news_ws',   'Polygon.io',       'api',  'wss://socket.polygon.io/...',  'business',   1,   '{"priority": 4, "requires_paid_key": true}'::jsonb)
--   ('businesswire_top',  'Business Wire',    'rss',  'https://www.businesswire.com/portal/site/home/rss/',  'business',  60,  '{"priority": 3}'::jsonb)
--   ('prnewswire_all',    'PR Newswire',      'rss',  'https://www.prnewswire.com/rss/news-releases-list.rss', 'business', 60,  '{"priority": 3}'::jsonb)
--   ('bls_news_releases', 'BLS',              'rss',  '<find current BLS RSS url>',   'macro',      120, '{"priority": 4}'::jsonb)
