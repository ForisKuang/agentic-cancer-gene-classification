"""
LLM-as-a-judge for semantic equivalence of gene summaries.
Uses Claude to score predicted summaries against Nicole's ground-truth summaries
on a 0–4 scale. Each score is accompanied by a short rationale.

Score rubric (from design doc):
  4 – Excellent: factually equivalent, same key claims, no contradictions
  3 – Good: mostly equivalent, minor omissions or different framing but correct core
  2 – Acceptable: some correct claims but missing significant content or framing
  1 – Poor: mostly different or containing factual discrepancies
  0 – Unacceptable: completely wrong, empty, or contradicts ground truth
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import anthropic

from src.config import settings

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

JUDGE_SYSTEM_PROMPT = """\
You are an expert cancer genomics curator evaluating the quality of LLM-generated gene summaries.
You will compare a predicted gene summary against a reference (ground-truth) summary produced by a
human expert curator. Score the predicted summary on a 0–4 scale.

Scoring rubric:
  4 – Excellent: factually equivalent to reference, same key mechanistic claims,
      no contradictions, all major cancer types mentioned, citations referenced correctly.
  3 – Good: mostly correct, same core findings, but minor omissions (one cancer type missed,
      one study not mentioned) or slightly different framing. No factual errors.
  2 – Acceptable: some correct claims but missing significant content (a key cancer type
      or mechanistic finding), OR present but with unclear framing. No contradictions.
  1 – Poor: mostly different from reference, or contains factual discrepancies or confabulation.
  0 – Unacceptable: completely wrong, empty, contradicts the reference, or hallucinates
      cancer associations that do not appear in the reference.

Be rigorous: a summary that captures the main driver claim and at least one cancer type context
but misses secondary studies should score 2–3. A summary with a major hallucinated mechanism
(e.g., claiming TSG when reference says OG) should score 0–1.

Respond with a JSON object: {"score": <int 0-4>, "rationale": "<one sentence>"}
"""


def _judge_one(gene: str, predicted: Optional[str], reference: Optional[str]) -> Dict:
    """Score a single predicted summary against the reference."""
    if not predicted and not reference:
        return {"score": 4, "rationale": "Both are empty — no summary expected."}
    if not predicted:
        return {"score": 0, "rationale": "Predicted summary is empty; reference has content."}
    if not reference:
        return {"score": 2, "rationale": "Reference is empty; predicted content cannot be verified."}

    prompt = (
        f"Gene: {gene}\n\n"
        f"Reference summary:\n{reference}\n\n"
        f"Predicted summary:\n{predicted}\n\n"
        "Score the predicted summary against the reference."
    )

    response = _client.messages.create(
        model=settings.synthesis_model,
        max_tokens=256,
        system=[
            {
                "type": "text",
                "text": JUDGE_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()

    # Parse JSON from response
    import json
    import re

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            score = int(result.get("score", 0))
            rationale = result.get("rationale", "")
            return {"score": max(0, min(4, score)), "rationale": rationale}
        except (json.JSONDecodeError, ValueError):
            pass

    logger.warning("Could not parse judge response for gene %s: %s", gene, text)
    return {"score": -1, "rationale": f"Parse error: {text[:100]}"}


def run_judge(
    genes: List[str],
    predicted_summaries: List[Optional[str]],
    reference_summaries: List[Optional[str]],
) -> Dict:
    """
    Run LLM-as-a-judge on all genes. Returns aggregate stats and per-gene scores.
    """
    per_gene = []
    scores = []

    for gene, pred, ref in zip(genes, predicted_summaries, reference_summaries):
        result = _judge_one(gene, pred, ref)
        score = result["score"]
        per_gene.append({"gene": gene, "score": score, "rationale": result["rationale"]})
        if score >= 0:
            scores.append(score)
        logger.info("Judge score for %s: %d — %s", gene, score, result["rationale"])

    n = len(scores)
    if n == 0:
        aggregate = {"mean_score": None, "excellent_pct": None, "acceptable_pct": None}
    else:
        mean = sum(scores) / n
        excellent = sum(1 for s in scores if s >= 3) / n
        acceptable = sum(1 for s in scores if s >= 2) / n
        aggregate = {
            "mean_score": round(mean, 3),
            "mean_pct": round(mean / 4 * 100, 1),
            "excellent_pct": round(excellent * 100, 1),   # score >= 3
            "acceptable_pct": round(acceptable * 100, 1), # score >= 2
            "n_evaluated": n,
        }

    return {"aggregate": aggregate, "per_gene": per_gene}
