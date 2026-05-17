-- 005_drop_unused_gin.sql
-- Drop the GIN index on events.classified.ticker_impact_weights created
-- in 003. No query in the codebase uses JSONB containment / key-existence
-- operators (@>, ?, ?|) on this column — the strategy reads weights from
-- the JSON cache (or a JSONB column SELECT, which doesn't use GIN). The
-- index was speculative; pays INSERT/UPDATE + storage cost for zero read
-- benefit. Drop now; trivially re-add when a real querying caller lands.

BEGIN;

DROP INDEX IF EXISTS events.idx_classified_ticker_weights;

COMMIT;
