"""
workers/tasks/quality.py — LLM-as-Judge Quality Evaluation Task

Called fire-and-forget after every inference (sampled at QUALITY_SAMPLE_RATE).
Replaces the hardcoded quality_score=1.0 in _fire_post_response_tasks() with
a real 0.0–1.0 score from an external judge model.

Pipeline:
  1. Call OpenRouter (Llama 3.3 70B, same free model) as judge.
  2. Score the response on accuracy, completeness, clarity (0–10 each).
  3. Normalise: quality_score = avg(a, c, cl) / 10.0
  4. Push the score to Langfuse for observability.
  5. Chain into update_mab_weights.delay() with the REAL quality_score.

Why chain instead of inline:
  update_mab_weights needs latency + cost + quality simultaneously.
  We receive latency and cost from the infer route immediately, but quality
  takes ~2-5s (judge API call). So the infer route fires run_quality_eval
  with all three values, and this task does the judge call THEN calls
  update_mab_weights with the final set.

Sampling (QUALITY_SAMPLE_RATE default 0.30):
  Only 30% of requests trigger this task to avoid hitting OpenRouter rate
  limits and to keep Celery queue pressure low. For unsampled requests,
  update_mab_weights is called immediately with quality_score=1.0.

tradeoffs-info §5: Judge model is an EXTERNAL API, never a local model.
"""

import asyncio
import base64
import json
import logging
import os

import httpx
import asyncpg

from workers.celery_app import celery_app

logger = logging.getLogger("tinai.tasks.quality")

_OR_API_KEY         = os.environ.get("OPENROUTER_API_KEY", "")
_JUDGE_MODEL        = "meta-llama/llama-3.3-70b-instruct"
_JUDGE_ENDPOINT     = "https://openrouter.ai/api/v1/chat/completions"

_LANGFUSE_SK        = os.environ.get("LANGFUSE_SECRET_KEY", "").strip('"')
_LANGFUSE_PK        = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip('"')
_LANGFUSE_HOST      = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com").strip('"')
_DATABASE_URL      = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")

_JUDGE_SYSTEM = (
    "You are a technical content quality evaluator. "
    "Score the AI response strictly as JSON, no other text."
)
_JUDGE_TEMPLATE = (
    "Score this AI response on three dimensions (0-10 each):\n"
    "1. Accuracy      — Factually correct and technically precise?\n"
    "2. Completeness  — Fully addresses all aspects of the question?\n"
    "3. Clarity       — Clearly written for a technical audience?\n\n"
    "Return ONLY JSON: "
    '{{"accuracy":<int>,"completeness":<int>,"clarity":<int>,"rationale":"<one sentence>"}}\n\n'
    "QUESTION: {prompt}\n\nANSWER: {output}"
)


def _call_judge(prompt: str, output: str) -> tuple[float, str]:
    """
    Synchronously call the judge model and return (quality_score, rationale).
    Falls back to 0.8 on any error (fail-safe: don't penalise MAB on judge failure).
    """
    judge_text = _JUDGE_TEMPLATE.format(
        prompt=prompt[:500],      # truncate to avoid token overshoot
        output=output[:2000],
    )
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                _JUDGE_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {_OR_API_KEY}",
                    "Content-Type":  "application/json",
                    "HTTP-Referer":  "https://github.com/sushk2904/tinai",
                    "X-Title":       "TINAI Quality Eval",
                },
                json={
                    "model":    _JUDGE_MODEL,
                    "messages": [
                        {"role": "system", "content": _JUDGE_SYSTEM},
                        {"role": "user",   "content": judge_text},
                    ],
                    "max_tokens": 120,
                },
            )
            resp.raise_for_status()
            raw     = resp.json()["choices"][0]["message"]["content"].strip()
            scores  = json.loads(raw)
            avg     = (scores["accuracy"] + scores["completeness"] + scores["clarity"]) / 30.0
            quality = round(max(0.0, min(1.0, avg)), 4)
            return quality, scores.get("rationale", "")

    except Exception as exc:
        logger.warning("Judge call failed (%s) — defaulting quality to 0.8", exc)
        return 0.8, "judge unavailable"


def _push_langfuse_score(request_id: str, quality: float, provider: str, model: str) -> None:
    """
    Push quality score to Langfuse via REST (no SDK dependency).

    Creates:
      - A trace linked to request_id
      - A numeric score named 'quality_score' on that trace
    """
    if not _LANGFUSE_PK or not _LANGFUSE_SK:
        return

    auth_header = "Basic " + base64.b64encode(
        f"{_LANGFUSE_PK}:{_LANGFUSE_SK}".encode()
    ).decode()
    headers = {"Authorization": auth_header, "Content-Type": "application/json"}

    try:
        with httpx.Client(timeout=10.0) as client:
            # Upsert trace so the score has something to attach to
            client.post(
                f"{_LANGFUSE_HOST}/api/public/traces",
                headers=headers,
                json={
                    "id":       request_id,
                    "name":     "tinai_inference",
                    "metadata": {"provider": provider, "model": model},
                    "tags":     [provider, "inference"],
                },
            )

            # Push the quality score
            client.post(
                f"{_LANGFUSE_HOST}/api/public/scores",
                headers=headers,
                json={
                    "traceId":  request_id,
                    "name":     "quality_score",
                    "value":    quality,
                    "comment":  f"LLM-as-judge via {_JUDGE_MODEL} | provider={provider}",
                    "dataType": "NUMERIC",
                },
            )
        logger.debug("Langfuse score pushed: request=%s quality=%.4f", request_id, quality)

    except Exception as exc:
        logger.warning("Langfuse push failed for %s: %s", request_id, exc)


@celery_app.task(
    name="workers.tasks.quality.run_quality_eval",
    bind=True,
    max_retries=1,
    acks_late=True,
)
def run_quality_eval(
    self,
    prompt:       str,
    output_text:  str,
    request_id:   str,
    provider:     str,
    model:        str,
    latency_ms:   int,
    cost_cents:   float,
) -> None:
    async def _update_db_quality(req_id: str, score: float):
        """Update the inference_logs row with the real quality score."""
        if not _DATABASE_URL:
             return
        try:
            conn = await asyncpg.connect(_DATABASE_URL)
            await conn.execute(
                "UPDATE inference_logs SET quality_score = $1 WHERE request_id = $2",
                score, req_id
            )
            await conn.close()
        except Exception as e:
            logger.warning("Failed to update log quality for %s: %s", req_id, e)
    """
    LLM-as-judge quality evaluation + MAB weight update.

    The infer route fires this instead of calling update_mab_weights directly
    when quality sampling is active. This task:
      1. Calls the judge model to get a real quality_score
      2. Pushes the score to Langfuse
      3. Calls update_mab_weights.delay() with the real quality_score

    Args:
        prompt:      Original user prompt (truncated in judge call).
        output_text: Provider's response to evaluate.
        request_id:  UUID linking to inference_logs and Langfuse trace.
        provider:    Provider name for MAB update and Langfuse metadata.
        model:       Model name for Langfuse metadata.
        latency_ms:  Wall-clock latency already recorded (passed to MAB update).
        cost_cents:  Cost already recorded (passed to MAB update).
    """
    try:
        # Step 1: LLM-as-judge scoring
        quality, rationale = _call_judge(prompt, output_text)
        logger.info(
            "Quality eval: request=%s provider=%s score=%.4f | %s",
            request_id, provider, quality, rationale[:80],
        )

        # Step 2: Push to Langfuse
        _push_langfuse_score(request_id, quality, provider, model)

        # Update the log row for dashboard visibility
        asyncio.run(_update_db_quality(request_id, quality))

        # Step 3: Chain into MAB weight update with REAL quality score
        from workers.tasks.telemetry import update_mab_weights
        update_mab_weights.delay(provider, latency_ms, cost_cents, quality)

    except Exception as exc:
        logger.error("Quality eval task failed for %s: %s — retrying", request_id, exc)
        raise self.retry(exc=exc, countdown=10)
