"""
workers/celery_app.py — Full Celery Application Bootstrap (Phase 3.1)

Replaces the Phase 1 stub with a production-ready Celery configuration.

CRITICAL (tradeoffs-info §1 + TODO §3.1):
  broker  = REDIS_URL_CELERY (DB 1) — NEVER use REDIS_URL_MAB (DB 0)
  backend = REDIS_URL_CELERY (DB 1) — isolates Celery queue backpressure
            from allkeys-lru MAB weight eviction.

Beat schedule (periodic tasks):
  update_mab_weights  — every 30s (rolling EMA refresh)
  run_drift_analysis  — 2 AM UTC nightly
  simulate_price_update — every 15 min (Phase 4.1)
"""

import os
from celery import Celery
from celery.schedules import crontab

celery_app = Celery(
    "tinai",
    broker=os.environ.get("REDIS_URL_CELERY", "redis://redis:6379/1"),
    backend=os.environ.get("REDIS_URL_CELERY", "redis://redis:6379/1"),
    # Auto-discover tasks in workers/tasks/ package
    include=[
        "workers.tasks.telemetry",
        "workers.tasks.safety",
        "workers.tasks.quality",
        "workers.tasks.drift",
        "workers.tasks.budget",
        "workers.tasks.cache",
        "workers.tasks.price_feed",
    ],
)

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # Timezone
    timezone="UTC",
    enable_utc=True,

    # Reliability: tasks are acknowledged AFTER execution, not before.
    # This prevents data loss if the worker crashes mid-task.
    task_acks_late=True,

    # Worker concurrency model — gevent/eventlet for I/O-bound Celery tasks
    # (asyncpg writes, Redis INCRBYFLOAT, external API calls for safety checks).
    # Default pool ('prefork') spawns separate processes; for I/O-heavy tasks
    # a single-process gevent pool handles 100s of concurrent tasks more efficiently.
    # Override with CELERY_WORKER_POOL env var if needed.
    worker_pool="prefork",

    # Task result TTL — results kept for 1h then auto-deleted.
    result_expires=3600,

    # Prevent tasks from running for more than 5 minutes (hard timeout).
    # Drift analysis is the longest task (~30s for 24h of data).
    task_time_limit=300,
    task_soft_time_limit=240,

    # Beat schedule — periodic tasks
    beat_schedule={
        # Nightly drift detection: 2 AM UTC
        # Queries last 24h of inference_logs and writes to drift_snapshots.
        "run-drift-analysis-nightly": {
            "task":     "workers.tasks.drift.run_drift_analysis",
            "schedule": crontab(hour=2, minute=0),
            "args":     [],
        },
        # Simulate price perturbations every 15 minutes (Phase 4.1)
        "simulate-dynamic-pricing": {
            "task":     "workers.tasks.price_feed.simulate_price_update",
            "schedule": crontab(minute="*/15"),
            "args":     [],
        },
    },
)
