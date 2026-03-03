"""
tests/test_infra.py — Phase 1.5 Connectivity Verification
TODO §1.5: One-shot test asserting Postgres and Redis are reachable
           from inside the api container network.

Run inside the api container:
    docker compose exec api python -m pytest tests/test_infra.py -v

What this tests (and why each matters):
  1. Postgres reachable via asyncpg (the driver the API uses at runtime).
  2. All four Alembic-managed tables exist — no silent schema drift.
  3. Redis DB 0 (REDIS_URL_MAB)   — control-plane operations work.
  4. Redis DB 1 (REDIS_URL_CELERY) — Celery broker is reachable.
  5. DB 0 and DB 1 are isolated   — a write to DB 0 is NOT visible in DB 1.
     This is the critical allkeys-lru isolation guarantee from tradeoffs-info §1.

These tests use no application logic — just raw driver calls. They exist
to validate infrastructure, not business rules.
"""

import asyncio
import os

import asyncpg
import pytest
import redis as redis_sync


# ---------------------------------------------------------------------------
# Configuration — read from the same env vars the API uses
# ---------------------------------------------------------------------------

# asyncpg uses the async URL; strip the "+asyncpg" driver qualifier for psycopg2
# style DSNs if present (asyncpg parses its own format).
DATABASE_URL   = os.environ["DATABASE_URL"]
REDIS_URL_MAB  = os.environ["REDIS_URL_MAB"]
REDIS_URL_CELERY = os.environ["REDIS_URL_CELERY"]

# Tables created by the initial_schema Alembic migration (Phase 1.3)
EXPECTED_TABLES = {
    "inference_logs",
    "provider_stats",
    "drift_snapshots",
    "client_budgets",
    "alembic_version",   # Alembic's own tracking table
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _asyncpg_url(url: str) -> str:
    """
    Convert a SQLAlchemy-style URL to asyncpg format.
    asyncpg does not accept the '+asyncpg' driver qualifier.
    e.g. postgresql+asyncpg://user:pass@host/db → postgresql://user:pass@host/db
    """
    return url.replace("postgresql+asyncpg://", "postgresql://")


# ---------------------------------------------------------------------------
# PostgreSQL tests
# ---------------------------------------------------------------------------

class TestPostgresConnectivity:

    def test_postgres_reachable(self):
        """
        Asserts a TCP connection + authentication to the Postgres container
        succeeds using asyncpg (the driver used by the FastAPI runtime).
        Failure here means: wrong DB URL, wrong credentials, or the postgres
        service is not on the tinai_net network visible from the api container.
        """
        async def _check():
            conn = await asyncpg.connect(_asyncpg_url(DATABASE_URL))
            result = await conn.fetchval("SELECT 1")
            await conn.close()
            return result

        result = asyncio.get_event_loop().run_until_complete(_check())
        assert result == 1, "Postgres did not return 1 from SELECT 1"

    def test_all_schema_tables_exist(self):
        """
        Asserts all four Phase 1.3 tables plus alembic_version exist.
        A missing table means `alembic upgrade head` was not run or
        the migration failed silently.
        """
        async def _check():
            conn = await asyncpg.connect(_asyncpg_url(DATABASE_URL))
            rows = await conn.fetch(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'public'
                ORDER BY tablename
                """
            )
            await conn.close()
            return {row["tablename"] for row in rows}

        found_tables = asyncio.get_event_loop().run_until_complete(_check())
        missing = EXPECTED_TABLES - found_tables
        assert not missing, (
            f"Missing tables in Postgres: {missing}. "
            "Run: docker compose exec api alembic upgrade head"
        )

    def test_inference_logs_indexes_exist(self):
        """
        Asserts the two critical indexes on inference_logs exist.
        Without these, drift queries at 1M rows/day will sequentially scan
        the entire table — exhausting Neon free-tier compute in 3 days
        (TODO §1.3 ⚠️ Sequential Scan Death Trap).
        """
        async def _check():
            conn = await asyncpg.connect(_asyncpg_url(DATABASE_URL))
            rows = await conn.fetch(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE tablename = 'inference_logs'
                  AND schemaname = 'public'
                """
            )
            await conn.close()
            return {row["indexname"] for row in rows}

        indexes = asyncio.get_event_loop().run_until_complete(_check())
        assert "ix_inference_logs_provider" in indexes, (
            "Missing index ix_inference_logs_provider on inference_logs. "
            "Phase 3.4 drift queries will full-scan the table."
        )
        assert "ix_inference_logs_created_at" in indexes, (
            "Missing index ix_inference_logs_created_at on inference_logs. "
            "Phase 6.2 time-window dashboard queries will full-scan the table."
        )


# ---------------------------------------------------------------------------
# Redis tests
# ---------------------------------------------------------------------------

class TestRedisConnectivity:

    def test_redis_db0_mab_reachable(self):
        """
        Asserts Redis DB 0 (REDIS_URL_MAB) is reachable.
        DB 0 is the control-plane store: MAB weights, L1 cache,
        rate-limit buckets, circuit-breaker state.
        """
        r = redis_sync.from_url(REDIS_URL_MAB, decode_responses=True)
        assert r.ping(), "Redis DB 0 (MAB) did not respond to PING"
        r.close()

    def test_redis_db1_celery_reachable(self):
        """
        Asserts Redis DB 1 (REDIS_URL_CELERY) is reachable.
        DB 1 is the Celery broker/backend store.
        """
        r = redis_sync.from_url(REDIS_URL_CELERY, decode_responses=True)
        assert r.ping(), "Redis DB 1 (Celery) did not respond to PING"
        r.close()

    def test_db0_and_db1_are_isolated(self):
        """
        CRITICAL: Asserts that DB 0 and DB 1 are truly isolated.
        A write to DB 0 must NOT be visible in DB 1.

        This validates the core allkeys-lru isolation guarantee from
        tradeoffs-info §1: Celery queue backpressure (DB 1) can never
        evict MAB keys (DB 0) because they live in separate logical databases.

        If this test fails: REDIS_URL_MAB and REDIS_URL_CELERY point to
        the same DB number — a critical misconfiguration.
        """
        probe_key   = "_tinai_infra_test_isolation_probe"
        probe_value = "db0_only"

        r0 = redis_sync.from_url(REDIS_URL_MAB,    decode_responses=True)
        r1 = redis_sync.from_url(REDIS_URL_CELERY, decode_responses=True)

        try:
            # Write to DB 0
            r0.set(probe_key, probe_value, ex=10)

            # Must be visible in DB 0
            assert r0.get(probe_key) == probe_value, (
                "Probe key not readable from DB 0 immediately after SET — "
                "Redis connection issue."
            )

            # Must NOT be visible in DB 1
            assert r1.get(probe_key) is None, (
                "CRITICAL: Probe key written to DB 0 is readable from DB 1. "
                "REDIS_URL_MAB and REDIS_URL_CELERY point to the same DB number. "
                "Fix .env: DB 0 → MAB, DB 1 → Celery."
            )
        finally:
            r0.delete(probe_key)
            r0.close()
            r1.close()

    def test_redis_db0_supports_key_namespaces(self):
        """
        Smoke-tests that real redis_keys.py builder functions produce
        keys that round-trip correctly through Redis DB 0.
        Verifies the namespace design from TODO §1.4 works end-to-end.
        """
        from api.redis_keys import (
            key_mab_weights,
            key_circuit_state,
            key_rate_limit,
            CIRCUIT_CLOSED,
        )

        r = redis_sync.from_url(REDIS_URL_MAB, decode_responses=True)

        test_keys = {
            key_mab_weights("groq"):       "0.75",
            key_circuit_state("groq"):     CIRCUIT_CLOSED,
            key_rate_limit("test_client"): "100",
        }

        try:
            for k, v in test_keys.items():
                r.set(k, v, ex=10)

            for k, expected in test_keys.items():
                actual = r.get(k)
                assert actual == expected, (
                    f"Redis round-trip failed for key {k!r}: "
                    f"expected {expected!r}, got {actual!r}"
                )
        finally:
            for k in test_keys:
                r.delete(k)
            r.close()
