"""
Scoring functions for M0 validation harness.
All metrics are implemented without external ML dependencies.

Fields evaluated:
  - cancer_associated      → binary accuracy + Cohen's kappa
  - cancer_associated_gene_tier → per-class F1 + macro F1
  - og_or_tsg              → per-class F1 + macro F1
  - citations              → set precision, recall, F1 per gene; then mean
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_div(num: float, den: float) -> float:
    return num / den if den > 0 else 0.0


# ---------------------------------------------------------------------------
# Cohen's kappa (binary)
# ---------------------------------------------------------------------------

def cohen_kappa_binary(
    pred: List[Optional[bool]],
    gold: List[Optional[bool]],
) -> Tuple[float, float]:
    """
    Compute observed accuracy and Cohen's kappa for binary (true/false) labels.
    None values in either pred or gold are skipped.
    Returns (accuracy, kappa).
    """
    pairs = [(p, g) for p, g in zip(pred, gold) if p is not None and g is not None]
    n = len(pairs)
    if n == 0:
        return 0.0, 0.0

    correct = sum(p == g for p, g in pairs)
    accuracy = correct / n

    # Expected agreement by chance
    pred_pos = sum(1 for p, _ in pairs if p is True) / n
    gold_pos = sum(1 for _, g in pairs if g is True) / n
    pred_neg = 1 - pred_pos
    gold_neg = 1 - gold_pos
    p_expected = pred_pos * gold_pos + pred_neg * gold_neg

    kappa = _safe_div(accuracy - p_expected, 1 - p_expected)
    return accuracy, kappa


# ---------------------------------------------------------------------------
# Per-class F1 (multiclass)
# ---------------------------------------------------------------------------

def per_class_f1(
    pred: List[Optional[str]],
    gold: List[Optional[str]],
) -> Tuple[Dict[str, float], float]:
    """
    Compute per-class F1 scores and macro-averaged F1.
    None values in either list are skipped.
    Returns (per_class_dict, macro_f1).
    """
    pairs = [(p, g) for p, g in zip(pred, gold) if p is not None and g is not None]

    classes = sorted({g for _, g in pairs})

    per_class: Dict[str, float] = {}
    for cls in classes:
        tp = sum(1 for p, g in pairs if p == cls and g == cls)
        fp = sum(1 for p, g in pairs if p == cls and g != cls)
        fn = sum(1 for p, g in pairs if p != cls and g == cls)
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        per_class[cls] = _safe_div(2 * precision * recall, precision + recall)

    macro = sum(per_class.values()) / len(per_class) if per_class else 0.0
    return per_class, macro


# ---------------------------------------------------------------------------
# Citation precision / recall / F1 (set-based)
# ---------------------------------------------------------------------------

def citation_scores(
    pred_citations: List[str],
    gold_citations: List[str],
) -> Tuple[float, float, float]:
    """
    Set-based precision, recall, F1 for a single gene's citations.
    Both lists are sets of PMID strings.
    """
    pred_set = set(pred_citations)
    gold_set = set(gold_citations)

    if not pred_set and not gold_set:
        return 1.0, 1.0, 1.0
    if not pred_set:
        return 0.0, 0.0, 0.0
    if not gold_set:
        return 0.0, 1.0, 0.0

    tp = len(pred_set & gold_set)
    precision = _safe_div(tp, len(pred_set))
    recall = _safe_div(tp, len(gold_set))
    f1 = _safe_div(2 * precision * recall, precision + recall)
    return precision, recall, f1


def mean_citation_scores(
    all_pred: List[List[str]],
    all_gold: List[List[str]],
) -> Dict[str, float]:
    """Aggregate citation scores across all genes."""
    scores = [citation_scores(p, g) for p, g in zip(all_pred, all_gold)]
    n = len(scores)
    if n == 0:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    return {
        "precision": sum(s[0] for s in scores) / n,
        "recall": sum(s[1] for s in scores) / n,
        "f1": sum(s[2] for s in scores) / n,
    }


# ---------------------------------------------------------------------------
# Summary-level report builder (non-LLM fields)
# ---------------------------------------------------------------------------

def compute_categorical_metrics(
    predictions: List[Dict],
    ground_truth: List[Dict],
) -> Dict:
    """
    Compare predicted GeneAnnotation dicts against ground-truth holdout dicts.
    Returns a metrics dict covering cancer_associated, tier, og_or_tsg, citations.
    """
    pred_cancer = [p.get("cancer_associated") for p in predictions]
    gold_cancer = [g.get("cancer_associated") for g in ground_truth]

    pred_tier = [p.get("cancer_associated_gene_tier") for p in predictions]
    gold_tier = [g.get("cancer_associated_gene_tier") for g in ground_truth]

    pred_ogtsg = [p.get("og_or_tsg") for p in predictions]
    gold_ogtsg = [g.get("og_or_tsg") for g in ground_truth]

    pred_cites = [p.get("citations", []) for p in predictions]
    gold_cites = [g.get("citations", []) for g in ground_truth]

    ca_accuracy, ca_kappa = cohen_kappa_binary(pred_cancer, gold_cancer)
    tier_per_class, tier_macro = per_class_f1(pred_tier, gold_tier)
    ogtsg_per_class, ogtsg_macro = per_class_f1(pred_ogtsg, gold_ogtsg)
    cite_scores = mean_citation_scores(pred_cites, gold_cites)

    return {
        "n": len(predictions),
        "cancer_associated": {
            "accuracy": round(ca_accuracy, 4),
            "cohen_kappa": round(ca_kappa, 4),
        },
        "cancer_tier": {
            "macro_f1": round(tier_macro, 4),
            "per_class": {k: round(v, 4) for k, v in tier_per_class.items()},
        },
        "og_or_tsg": {
            "macro_f1": round(ogtsg_macro, 4),
            "per_class": {k: round(v, 4) for k, v in ogtsg_per_class.items()},
        },
        "citations": {k: round(v, 4) for k, v in cite_scores.items()},
    }
