-- 006_event_triggers.sql — AFTER INSERT triggers that pg_notify on new events.
--
-- The trader dispatcher (trader/dispatcher.py) holds one autocommit connection
-- and LISTENs on three channels. Each trigger fires a notify with the new
-- row's primary key as the payload — the dispatcher then SELECTs the full
-- row, resolves tickers, filters against signals.watchlist, and dispatches
-- to the fast/slow signal paths.
--
-- Channels:
--   article_in  ← news.articles      payload = article_id (UUID)
--   post_in     ← social.posts       payload = post_id    (UUID)
--   macro_in    ← macro.releases     payload = release_id (UUID)
--
-- pg_notify payloads are capped at 8000 bytes; sending just the id keeps us
-- comfortably under and avoids any chance of truncation on large news bodies.

-- ── news.articles ──────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION news.notify_article_in()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('article_in', NEW.article_id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_news_articles_notify ON news.articles;
CREATE TRIGGER trg_news_articles_notify
    AFTER INSERT ON news.articles
    FOR EACH ROW EXECUTE FUNCTION news.notify_article_in();

-- ── social.posts ───────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION social.notify_post_in()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('post_in', NEW.post_id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_social_posts_notify ON social.posts;
CREATE TRIGGER trg_social_posts_notify
    AFTER INSERT ON social.posts
    FOR EACH ROW EXECUTE FUNCTION social.notify_post_in();

-- ── macro.releases ─────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION macro.notify_release_in()
RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify('macro_in', NEW.release_id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_macro_releases_notify ON macro.releases;
CREATE TRIGGER trg_macro_releases_notify
    AFTER INSERT ON macro.releases
    FOR EACH ROW EXECUTE FUNCTION macro.notify_release_in();
