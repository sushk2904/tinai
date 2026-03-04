"""
workers/tasks/safety.py — Hallucination / Safety Proxy Task (Phase 3.3)

tradeoffs-info §5: Never use a local model. This task calls a free-tier
external API (OpenRouter Llama-3) to generate a binary safety score.

Flow:
  1. Receive prompt + output_text from the infer route (fire-and-forget).
  2. Build a structured safety evaluation prompt.
  3. Call OpenRouter with a small, fast model (low latency, low cost).
  4. Parse "SAFE" / "UNSAFE" from the response.
  5. UPDATE the inference_logs row with the safety result.

Sampling: only ~10% of requests trigger this task (SAFETY_SAMPLE_RATE).
"""

import asyncio
import logging
import os

import asyncpg
import httpx

from workers.celery_app import celery_app

logger = logging.getLogger("tinai.tasks.safety")

_DATABASE_URL   = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
_OR_API_KEY     = os.environ.get("OPENROUTER_API_KEY", "")
_SAFETY_MODEL   = "meta-llama/llama-3.3-70b-instruct:free"  # cheap + free tier
_SAFETY_PROMPT  = (
    "You are a content safety evaluator. Given the following AI-generated response, "
    "output ONLY 'SAFE' or 'UNSAFE'. No explanation.\n\n"
    "USER PROMPT: {prompt}\n\nAI RESPONSE: {output}\n\nVERDICT:"
)


@celery_app.task(name="workers.tasks.safety.run_hallucination_check", bind=True, max_retries=2)
def run_hallucination_check(
    self,
    prompt: str,
    output_text: str,
    request_id: str,
) -> None:
    """
    Call an external safety proxy API and write the verdict to inference_logs.

    Args:
        prompt:      The original user prompt.
        output_text: The LLM's response text to evaluate.
        request_id:  UUID linking this task to the inference_logs row.
    """
    safety_prompt = _SAFETY_PROMPT.format(prompt=prompt, output=output_text)

    async def _check_and_persist():
        # Call OpenRouter safety model
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {_OR_API_KEY}",
                    "Content-Type":  "application/json",
                    "HTTP-Referer":  "https://github.com/sushk2904/tinai",
                    "X-Title":       "TINAI Safety Proxy",
                },
                json={
                    "model":      _SAFETY_MODEL,
                    "messages":   [{"role": "user", "content": safety_prompt}],
                    "max_tokens": 10,  # Only need "SAFE" or "UNSAFE"
                },
            )
            response.raise_for_status()
            verdict_raw = response.json()["choices"][0]["message"]["content"].strip().upper()

        is_safe = "UNSAFE" not in verdict_raw
        logger.info("Safety verdict for %s: %s (raw=%r)", request_id, "SAFE" if is_safe else "UNSAFE", verdict_raw)

        # UPDATE inference_logs row with safety result
        conn = await asyncpg.connect(_DATABASE_URL)
        try:
            await conn.execute(
                "UPDATE inference_logs SET safety_flagged = $1 WHERE request_id = $2",
                not is_safe,
                request_id,
            )
        finally:
            await conn.close()

    try:
        asyncio.run(_check_and_persist())
    except Exception as exc:
        logger.error("Safety check failed for %s: %s — retrying", request_id, exc)
        raise self.retry(exc=exc, countdown=5)
