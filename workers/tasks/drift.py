"""
workers/tasks/drift.py — Semantic Drift Detection Nightly Batch (Phase 3.4)

Runs nightly at 2 AM UTC via Celery Beat.

Pipeline:
  1. Query last 24h of inference_logs rows from Postgres.
  2. Load the golden reference distribution from workers/golden_set.json.
  3. Run Evidently DataDriftPreset on the output_text column.
  4. Write a DriftSnapshot row to Postgres with the drift score.

TODO §3.4: Register as crontab(hour=2, minute=0) in celery_app.py beat_schedule.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

from workers.celery_app import celery_app

logger = logging.getLogger("tinai.tasks.drift")

_DATABASE_URL = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")

# Path to the golden reference set (relative to /app in the container)
_GOLDEN_SET_PATH = Path(__file__).parent.parent / "golden_set.json"


@celery_app.task(name="workers.tasks.drift.run_drift_analysis", bind=True, max_retries=1)
def run_drift_analysis(self, run_date: str = "") -> None:
    """
    Nightly semantic drift detection task.

    Args:
        run_date: ISO date string (YYYY-MM-DD) for the analysis window.
                  Defaults to yesterday (the completed 24h window).

    Writes one row to drift_snapshots per provider with the drift score.
    """
    try:
        import pandas as pd
        from evidently.report import Report
        from evidently.metric_preset import DataDriftPreset
        from evidently import ColumnMapping
    except ImportError as e:
        logger.error("evidently/pandas not installed — skipping drift analysis: %s", e)
        return

    async def _run():
        # Determine analysis window
        now_utc   = datetime.now(timezone.utc)
        window_end   = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        window_start = window_end - timedelta(hours=24)

        conn = await asyncpg.connect(_DATABASE_URL)
        try:
            rows = await conn.fetch(
                """
                SELECT provider, output_text
                FROM inference_logs
                WHERE created_at >= $1
                  AND created_at <  $2
                  AND error_flag  = FALSE
                  AND output_text IS NOT NULL
                ORDER BY provider, created_at
                """,
                window_start,
                window_end,
            )
        finally:
            await conn.close()

        if not rows:
            logger.warning("Drift analysis: no inference data found for %s — skipping.", window_start.date())
            return

        # Load golden reference set
        if not _GOLDEN_SET_PATH.exists():
            logger.warning("Golden set not found at %s — skipping drift analysis.", _GOLDEN_SET_PATH)
            return

        with open(_GOLDEN_SET_PATH) as f:
            golden_data = json.load(f)

        reference_df = pd.DataFrame(golden_data)
        if "output_text" not in reference_df.columns:
            logger.error("Golden set must have 'output_text' column.")
            return

        # Group by provider and run drift per provider
        all_rows = [dict(r) for r in rows]
        df_all   = pd.DataFrame(all_rows)

        providers = df_all["provider"].unique()
        snapshots: list[dict] = []

        for provider in providers:
            current_df = df_all[df_all["provider"] == provider][["output_text"]].reset_index(drop=True)

            if len(current_df) < 10:
                logger.warning("Drift: only %d rows for %s — skipping (need ≥10).", len(current_df), provider)
                continue

            column_mapping = ColumnMapping(text_features=["output_text"])
            report = Report(metrics=[DataDriftPreset()])
            report.run(
                reference_data=reference_df[["output_text"]],
                current_data=current_df,
                column_mapping=column_mapping,
            )

            result       = report.as_dict()
            drift_score  = result["metrics"][0]["result"].get("dataset_drift_score", 0.0)
            sample_count = len(current_df)

            snapshots.append({
                "provider":     provider,
                "drift_score":  drift_score,
                "sample_count": sample_count,
                "run_date":     window_start.date(),
            })
            logger.info("Drift[%s] score=%.4f samples=%d", provider, drift_score, sample_count)

        # Write snapshots to Postgres
        if snapshots:
            conn = await asyncpg.connect(_DATABASE_URL)
            try:
                await conn.executemany(
                    """
                    INSERT INTO drift_snapshots (provider, drift_score, sample_count, run_date)
                    VALUES ($1, $2, $3, $4)
                    """,
                    [(s["provider"], s["drift_score"], s["sample_count"], s["run_date"])
                     for s in snapshots],
                )
            finally:
                await conn.close()
            logger.info("Drift analysis complete: %d snapshots written.", len(snapshots))

    try:
        asyncio.run(_run())
    except Exception as exc:
        logger.error("Drift analysis failed: %s — retrying", exc)
        raise self.retry(exc=exc, countdown=300)
