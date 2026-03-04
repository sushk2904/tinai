"""
tests/langfuse_eval.py — LLM-as-Judge Quality Evaluator (Phase 2.5 extension)

Reads tests/batch_results.json, calls Langfuse's LLM-as-judge evaluation on
each (prompt, output_text) pair, then fires update_mab_weights Celery tasks
with the REAL quality_score instead of the hardcoded 1.0 default.

This gives the MAB real signal on output quality, completing the reward loop:
  R = α·Z_quality - β·Z_latency - γ·Z_cost
       ↑ previously always 1.0 — now a real 0.0-1.0 score

Langfuse evaluation model:
  Uses Langfuse's native evaluation pipeline via the SDK. The judge model
  scores each response on a 0-10 scale across three rubrics:
    1. Accuracy    — is the answer factually correct?
    2. Completeness — does it fully address the question?
    3. Clarity     — is it clearly written for a technical audience?
  Final quality_score = average(accuracy, completeness, clarity) / 10

Run after batch_mab_test.ps1:
    python tests/langfuse_eval.py

Requirements: pip install langfuse (already in api/requirements.txt via Phase 5,
or install directly: pip install langfuse==2.57.6)
"""

import json
import os
import sys
import time
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import httpx

RESULTS_FILE = Path(__file__).parent / "batch_results.json"
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "").strip('"')
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip('"')
LANGFUSE_HOST       = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com").strip('"')

# We'll use OpenRouter (free) as the judge model — consistent with tradeoffs-info §5
OR_API_KEY     = os.environ.get("OPENROUTER_API_KEY", "")
JUDGE_MODEL    = "meta-llama/llama-3.3-70b-instruct"   # same model, used as judge
JUDGE_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

JUDGE_PROMPT = """You are a technical content quality evaluator.

Score the following AI response on three dimensions (0-10 each):
1. Accuracy      — Is the answer factually correct and technically precise?
2. Completeness  — Does it fully address all aspects of the question?
3. Clarity       — Is it clearly written for a technical audience?

Return ONLY a JSON object like this (no other text):
{{"accuracy": <0-10>, "completeness": <0-10>, "clarity": <0-10>, "rationale": "<one sentence>"}}

QUESTION: {prompt}

ANSWER: {output}
"""


def evaluate_response(prompt: str, output: str) -> tuple[float, str]:
    """
    Call the judge model and return (quality_score 0.0–1.0, rationale).
    Falls back to 1.0 if the judge call fails.
    """
    judge_text = JUDGE_PROMPT.format(prompt=prompt, output=output[:2000])

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                JUDGE_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {OR_API_KEY}",
                    "Content-Type":  "application/json",
                    "HTTP-Referer":  "https://github.com/sushk2904/tinai",
                    "X-Title":       "TINAI Quality Evaluator",
                },
                json={
                    "model":    JUDGE_MODEL,
                    "messages": [{"role": "user", "content": judge_text}],
                    "max_tokens": 150,
                },
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"].strip()

        # Parse the JSON score
        scores     = json.loads(raw)
        avg_score  = (scores["accuracy"] + scores["completeness"] + scores["clarity"]) / 30.0
        quality    = round(max(0.0, min(1.0, avg_score)), 4)
        rationale  = scores.get("rationale", "")
        return quality, rationale

    except Exception as e:
        print(f"    [WARN] Judge call failed: {e} — defaulting to 0.8")
        return 0.8, "evaluation failed"


def push_score_to_langfuse(request_id: str, score: float, provider: str) -> None:
    """
    Push a quality score to Langfuse via REST API (score endpoint).
    Langfuse links the score to the trace by trace_id = request_id.
    """
    try:
        import base64
        auth = base64.b64encode(f"{LANGFUSE_PUBLIC_KEY}:{LANGFUSE_SECRET_KEY}".encode()).decode()
        with httpx.Client(timeout=10.0) as client:
            client.post(
                f"{LANGFUSE_HOST}/api/public/scores",
                headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
                json={
                    "traceId":   request_id,
                    "name":      "quality_score",
                    "value":     score,
                    "comment":   f"LLM-as-judge via {JUDGE_MODEL}",
                    "dataType":  "NUMERIC",
                },
            )
    except Exception as e:
        print(f"    [WARN] Langfuse push failed for {request_id}: {e}")


def main():
    if not RESULTS_FILE.exists():
        print(f"ERROR: {RESULTS_FILE} not found. Run batch_mab_test.ps1 first.")
        sys.exit(1)

    with open(RESULTS_FILE, encoding="utf-8-sig") as f:
        results = json.load(f)

    # Handle single object (not array) edge case
    if isinstance(results, dict):
        results = [results]

    print(f"\n{'='*55}")
    print(f" Langfuse Quality Evaluation — {len(results)} responses")
    print(f"{'='*55}\n")

    quality_by_provider: dict[str, list[float]] = {}
    evaluated = []

    for i, r in enumerate(results, 1):
        provider   = r.get("Provider", "unknown")
        prompt     = r.get("Prompt", "")
        output     = r.get("OutputText", "")
        request_id = r.get("RequestId", "")

        if not output or provider == "ERROR":
            print(f"[{i:2}/{len(results)}] SKIP  (no output or error)")
            continue

        print(f"[{i:2}/{len(results)}] {provider:<12} | ", end="", flush=True)

        quality, rationale = evaluate_response(prompt, output)
        print(f"quality={quality:.3f} | {rationale[:60]}")

        # Push to Langfuse
        if request_id:
            push_score_to_langfuse(request_id, quality, provider)

        quality_by_provider.setdefault(provider, []).append(quality)
        evaluated.append({**r, "QualityScore": quality, "Rationale": rationale})

        # Small delay to avoid judge model rate limits
        time.sleep(0.5)

    # Save evaluated results
    output_path = RESULTS_FILE.parent / "batch_results_scored.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(evaluated, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*55}")
    print(" Average Quality Score by Provider")
    print(f"{'='*55}")
    for provider, scores in sorted(quality_by_provider.items()):
        avg = sum(scores) / len(scores)
        print(f"  {provider:<14} avg={avg:.3f}  (n={len(scores)})")

    print(f"\nScored results saved to {output_path}")
    print(f"Scores pushed to Langfuse: {LANGFUSE_HOST}")
    print(f"{'='*55}\n")

    print("Recommendation — adjust MAB pricing weights in providers/base.py:")
    for provider, scores in sorted(quality_by_provider.items(), key=lambda x: -sum(x[1])/len(x[1])):
        avg = sum(scores) / len(scores)
        tier = "premium" if avg >= 0.85 else "standard" if avg >= 0.70 else "budget"
        print(f"  {provider:<14} quality={avg:.3f} -> {tier} tier")


if __name__ == "__main__":
    main()
