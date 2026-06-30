"""Unit tests for benchmark scoring functions."""

from benchmarks.metrics import (
    cohen_kappa_binary,
    citation_scores,
    mean_citation_scores,
    per_class_f1,
    compute_categorical_metrics,
)


def test_cohen_kappa_perfect_agreement():
    pred = [True, False, True, True, False]
    gold = [True, False, True, True, False]
    acc, kappa = cohen_kappa_binary(pred, gold)
    assert acc == 1.0
    assert kappa == 1.0


def test_cohen_kappa_no_agreement():
    # Mixed predictions that are all wrong — kappa should be negative or zero
    pred = [True, False, True, False]
    gold = [False, True, False, True]
    acc, kappa = cohen_kappa_binary(pred, gold)
    assert acc == 0.0
    assert kappa <= 0


def test_cohen_kappa_skips_none():
    pred = [True, None, False]
    gold = [True, True, False]
    acc, kappa = cohen_kappa_binary(pred, gold)
    # Only 2 pairs evaluated (True/True and False/False)
    assert acc == 1.0


def test_citation_scores_exact_match():
    p, r, f1 = citation_scores(["111", "222", "333"], ["111", "222", "333"])
    assert p == 1.0 and r == 1.0 and f1 == 1.0


def test_citation_scores_partial():
    p, r, f1 = citation_scores(["111", "222"], ["111", "333"])
    assert p == 0.5   # 1 of 2 predicted are correct
    assert r == 0.5   # 1 of 2 gold are found
    assert abs(f1 - 0.5) < 1e-6


def test_citation_scores_empty_both():
    p, r, f1 = citation_scores([], [])
    assert p == 1.0 and r == 1.0 and f1 == 1.0


def test_citation_scores_empty_pred():
    p, r, f1 = citation_scores([], ["111"])
    assert p == 0.0 and r == 0.0 and f1 == 0.0


def test_per_class_f1_binary():
    pred = ["OG", "TSG", "OG", "TSG"]
    gold = ["OG", "OG", "TSG", "TSG"]
    per_class, macro = per_class_f1(pred, gold)
    # 1 TP for OG (pred=OG,gold=OG), 1 FP (pred=OG,gold=TSG), 1 FN (pred=TSG,gold=OG)
    # OG: P=0.5 R=0.5 F1=0.5
    # TSG: P=0.5 R=0.5 F1=0.5
    assert per_class["OG"] == 0.5
    assert per_class["TSG"] == 0.5
    assert macro == 0.5


def test_per_class_f1_perfect():
    pred = ["OG", "TSG", "OG"]
    gold = ["OG", "TSG", "OG"]
    per_class, macro = per_class_f1(pred, gold)
    assert per_class["OG"] == 1.0
    assert per_class["TSG"] == 1.0
    assert macro == 1.0


def test_compute_categorical_metrics_smoke():
    predictions = [
        {"cancer_associated": True, "cancer_associated_gene_tier": "Class II - Likely Driver",
         "og_or_tsg": "OG", "citations": ["111", "222"]},
        {"cancer_associated": False, "cancer_associated_gene_tier": None,
         "og_or_tsg": None, "citations": []},
    ]
    ground_truth = [
        {"cancer_associated": True, "cancer_associated_gene_tier": "Class II - Likely Driver",
         "og_or_tsg": "OG", "citations": ["111", "222", "333"]},
        {"cancer_associated": False, "cancer_associated_gene_tier": None,
         "og_or_tsg": None, "citations": []},
    ]
    metrics = compute_categorical_metrics(predictions, ground_truth)
    assert metrics["n"] == 2
    assert metrics["cancer_associated"]["accuracy"] == 1.0
    assert metrics["cancer_associated"]["cohen_kappa"] == 1.0
    assert metrics["citations"]["precision"] == 1.0  # 2/2 predicted are in gold
    assert metrics["citations"]["recall"] < 1.0      # 2/3 gold found
