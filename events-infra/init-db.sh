#!/bin/bash
set -e

echo "=== Applying repo migrations ==="
for f in /migrations/repo/*.sql; do
    echo "  -> $(basename "$f")"
    psql -U aotc -d aotc -f "$f"
done

echo "=== Applying events-infra migrations ==="
for f in /migrations/events/*.sql; do
    echo "  -> $(basename "$f")"
    psql -U aotc -d aotc -f "$f"
done

echo "=== All migrations applied ==="
