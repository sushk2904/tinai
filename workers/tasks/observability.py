"""
workers/tasks/observability.py — Observation and Tracing Tasks (Phase 5)

Contains async jobs to publish LLM telemetry outward to observing tools 
(Langfuse) without blocking the hot inference path.
"""

import logging
from api.config import get_settings
from workers.celery_app import celery_app

logger = logging.getLogger("tinai.tasks.observability")
settings = get_settings()

@celery_app.task(name="workers.tasks.observability.send_langfuse_trace", bind=True, max_retries=3)
def send_langfuse_trace(
    self,
    request_id: str,
    prompt: str,
    output_text: str,
    provider: str,
    latency_ms: int,
    cost_cents: float,
    model: str,
) -> None:
    """
    Fire-and-forget task to sink prompt/completion data to Langfuse cloud instance.
    Initializes a short-lived Langfuse client per task, sends spans, and flushes 
    synchronously before exit.
    """
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.debug(f"Langfuse not configured. Skipping trace for {request_id}")
        return

    # Defer import to avoid making workers fail fast if Langfuse isn't pip-installed
    from langfuse import Langfuse

    try:
        # Reinitialize connection statelessly. Langfuse uses an embedded background thread,
        # so flush() MUST be called to synchronize it before the task unloads.
        langfuse = Langfuse(
            secret_key=settings.langfuse_secret_key,
            public_key=settings.langfuse_public_key,
            host=settings.langfuse_host,
        )
        
        trace = langfuse.trace(
            id=request_id,
            name="inference_mab_route",
            tags=[f"provider:{provider}", f"model:{model}"],
        )

        trace.generation(
            name="llm_generation",
            model=model,
            input=prompt,
            output=output_text,
            metadata={
                "latency_ms": latency_ms,
                "cost_cents": cost_cents,
                "provider": provider,
            }
        )
        
        langfuse.flush()
        logger.info(f"Langfuse trace exported successfully: {request_id}")

    except Exception as exc:
        logger.error("Failed to export Langfuse trace: %s", exc)
        raise self.retry(exc=exc, countdown=5)
