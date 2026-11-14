"""Vectorized, tie-corrected, two-sided asymptotic Mann–Whitney statistics."""

import re
import warnings

import numpy as np
from scipy.special import ndtr
from scipy.stats import hypergeom, rankdata

from . import PARAMS

# Deliberately conservative lineage-marker examples; informational, not an exclusion.
LINEAGE = {
    "LIHC": {"ALB", "APOA1", "APOA2", "TTR", "FGA", "FGB", "FGG"},
    "PRAD": {"KLK3", "KLK2", "ACPP"},
    "THCA": {"TG", "TPO"},
    "SKCM": {"MLANA", "PMEL", "TYR"},
    "UVM": {"MLANA", "PMEL", "TYR"},
}


def primary_indices(samples, code, seen=None):
    """Deterministically keep the lexicographically first primary per patient."""
    seen = set() if seen is None else seen
    selected = []
    for i in sorted(range(len(samples)), key=lambda i: samples[i]):
        match = re.fullmatch(r"(TCGA-[A-Z0-9]{2}-[A-Z0-9]{4})-(\d{2})(?:[A-Z0-9-]*)", samples[i])
        if match and match[2] == ("03" if code == "LAML" else "01") and match[1] not in seen:
            seen.add(match[1])
            selected.append(i)
    return selected


def bh(p):
    """BH along the final axis, including unavailable comparisons as p=1."""
    p = np.nan_to_num(np.asarray(p, dtype=float), nan=1.0)
    order = np.argsort(p, axis=-1)
    ordered = np.take_along_axis(p, order, axis=-1)
    adjusted = ordered * p.shape[-1] / np.arange(1, p.shape[-1] + 1)
    adjusted = np.minimum.accumulate(adjusted[..., ::-1], axis=-1)[..., ::-1]
    return np.take_along_axis(np.minimum(adjusted, 1), np.argsort(order, axis=-1), axis=-1)


def passes(q, fc, delta, median, n, params=PARAMS):
    return (
        (q < params["fdr"])
        & (fc >= params["log2fc"])
        & (delta >= params["cliffs_delta"])
        & (median >= params["expression_floor_log2"])
        & (n >= params["min_n"])
    )


def subgroup_pass(result, params=PARAMS):
    """Base v3 subgroup gate, independent of the lineage arm."""
    return (
        (result["n"] >= params["min_n"])
        & (result["n_rest"] >= params["min_n"])
        & (result["subgroup_high_count"] >= params["subgroup_min_count"])
        & (result["subgroup_high_fraction"] >= params["subgroup_min_fraction"])
        & (result["subgroup_high_fraction_ratio"] >= params["subgroup_min_fraction_ratio"])
        & (result["p95_log2_diff"] >= params["subgroup_p95_log2_diff"])
        & (result["subgroup_q"] < params["subgroup_fdr"])
    )


def compute(linear, labels, codes, params=PARAMS):
    """Return gene × type arrays. Input genes × samples; NaN means unmeasured.

    All 32 comparisons share pooled ranks and tie corrections. No per-gene tests.
    Call in bounded gene batches; zeros are measured, missing values are omitted.
    """
    x = np.asarray(linear, dtype=np.float64)
    if np.any(np.isinf(x)) or np.any(x <= -1):
        raise ValueError("Expression must be greater than -1 and finite or NaN")
    x = np.log2(x + 1)
    labels = np.asarray(labels)
    if len(labels) != x.shape[1] or set(labels) - set(codes):
        raise ValueError("Sample labels must match the supplied cancer types")
    ranks = np.nan_to_num(rankdata(x, axis=1, nan_policy="omit"))
    total = np.sum(np.isfinite(x), axis=1)
    # Count tied runs in sorted rows; NaNs compare unequal and contribute zero.
    ordered = np.sort(x, axis=1)
    starts = np.ones_like(ordered, dtype=bool)
    starts[:, 1:] = ordered[:, 1:] != ordered[:, :-1]
    rows, columns = np.nonzero(starts)
    flat = rows * x.shape[1] + columns
    lengths = np.diff(np.append(flat, x.size)).astype(float)
    ties = np.bincount(rows, weights=lengths**3 - lengths, minlength=len(x))
    shape = (len(x), len(codes))
    result = {
        key: np.full(shape, np.nan)
        for key in (
            "n",
            "n_rest",
            "median_log2",
            "p90_log2",
            "rest_p90_log2",
            "rest_median_log2",
            "log2fc",
            "log2fc_vs_median_of_type_medians",
            "cliffs_delta",
            "auc",
            "p_value",
        )
    }
    medians = np.full(shape, np.nan)
    detection = np.zeros(shape)
    for key in (
        "subgroup_high_count",
        "subgroup_high_fraction",
        "subgroup_high_fraction_ratio",
        "p95_log2_diff",
        "subgroup_p",
    ):
        result[key] = np.full(shape, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        threshold = np.maximum(
            np.nanpercentile(x, params["subgroup_percentile"], axis=1),
            params["expression_floor_log2"],
        )
        high = x > threshold[:, None]
        high_total = high.sum(axis=1)
        for j, code in enumerate(codes):
            group = labels == code
            if np.any(group):
                medians[:, j] = np.nanmedian(x[:, group], axis=1)
        for j, code in enumerate(codes):
            group = labels == code
            n = np.sum(np.isfinite(x[:, group]), axis=1)
            rest = total - n
            u = ranks[:, group].sum(axis=1) - n * (n + 1) / 2
            pairs = n * rest
            variance = pairs / 12 * (total + 1 - ties / (total * (total - 1)))
            z = np.maximum(np.abs(u - pairs / 2) - 0.5, 0) / np.sqrt(variance)
            p = 2 * ndtr(-z)
            p = np.where(variance == 0, 1.0, p)
            p = np.where(pairs > 0, p, np.nan)
            auc = np.where(pairs > 0, u / pairs, np.nan)
            rest_median = (
                np.nanmedian(x[:, ~group], axis=1) if np.any(~group) else np.full(len(x), np.nan)
            )
            k = high[:, group].sum(axis=1)
            k_rest = high_total - k
            detection[:, j] = (x[:, group] >= params["expression_floor_log2"]).sum(axis=1) / n
            p95 = (
                np.nanpercentile(x[:, group], params["subgroup_p95_percentile"], axis=1)
                if np.any(group)
                else np.full(len(x), np.nan)
            )
            rest_p95 = (
                np.nanpercentile(x[:, ~group], params["subgroup_p95_percentile"], axis=1)
                if np.any(~group)
                else np.full(len(x), np.nan)
            )
            values = dict(
                subgroup_high_count=np.where(n > 0, k, np.nan),
                subgroup_high_fraction=k / n,
                subgroup_high_fraction_ratio=np.where(
                    rest > 0, (k / n) / (np.maximum(k_rest, 1) / rest), np.nan
                ),
                p95_log2_diff=np.maximum(p95, params["expression_floor_log2"])
                - np.maximum(rest_p95, params["expression_floor_log2"]),
                subgroup_p=np.where(pairs > 0, hypergeom.sf(k - 1, total, high_total, n), np.nan),
                n=n,
                n_rest=rest,
                median_log2=medians[:, j],
                rest_median_log2=rest_median,
                p90_log2=(
                    np.nanpercentile(x[:, group], 90, axis=1)
                    if np.any(group)
                    else np.full(len(x), np.nan)
                ),
                rest_p90_log2=(
                    np.nanpercentile(x[:, ~group], 90, axis=1)
                    if np.any(~group)
                    else np.full(len(x), np.nan)
                ),
                log2fc=np.maximum(medians[:, j], params["expression_floor_log2"])
                - np.maximum(rest_median, params["expression_floor_log2"]),
                log2fc_vs_median_of_type_medians=np.maximum(
                    medians[:, j], params["expression_floor_log2"]
                )
                - np.maximum(
                    np.nanmedian(np.delete(medians, j, axis=1), axis=1),
                    params["expression_floor_log2"],
                ),
                cliffs_delta=2 * auc - 1,
                auc=auc,
                p_value=p,
            )
            for key, value in values.items():
                result[key][:, j] = value
    result["q_value"] = np.where(np.isfinite(result["p_value"]), bh(result["p_value"]), np.nan)
    lineage_v2 = passes(
        result["q_value"],
        result["log2fc"],
        result["cliffs_delta"],
        result["median_log2"],
        result["n"],
        params,
    )
    with np.errstate(invalid="ignore", divide="ignore"):
        linear_medians = np.exp2(medians) - 1
        cv = np.std(linear_medians, axis=1) / np.mean(linear_medians, axis=1)
    broad = (
        (len(codes) == params["broad_required_types"])
        & np.all(result["n"] >= params["min_n"], axis=1)
        & ((detection >= params["broad_sample_fraction"]).sum(axis=1) >= params["broad_min_types"])
        & (cv <= params["broad_type_median_cv"])
    )
    result["broadly_expressed"] = broad
    result["lineage_blocked_by_broad"] = lineage_v2 & broad[:, None]
    lineage = lineage_v2 & ~broad[:, None]
    p = result.pop("subgroup_p")
    result["subgroup_q"] = np.where(np.isfinite(p), bh(p), np.nan)
    subgroup = subgroup_pass(result, params)
    result["stats_pass"] = lineage | subgroup
    result["pass_mode"] = np.where(lineage, "lineage", np.where(subgroup, "subgroup", None))
    result["confidence_tier"] = confidence_tier(result, lineage, subgroup, params)
    result["strong_arm"] = strong_arm(result, lineage, subgroup, params)
    return result


def strong_masks(result, lineage, subgroup, params=PARAMS):
    """Strong-cutoff masks per arm; the lineage mask keeps its broad-expression block.

    The subgroup strong criteria are evaluated whether or not the pair's pass_mode is
    subgroup, so a lineage-first pass can still be promoted. They still require the
    base subgroup gate (counts, P95 shift and FDR), not just fraction and ratio.
    """
    with np.errstate(invalid="ignore"):
        strong_lineage = (
            lineage
            & (result["log2fc"] >= params["strong_log2fc"])
            & (result["cliffs_delta"] >= params["strong_cliffs_delta"])
        )
        strong_subgroup = (
            subgroup
            & (result["subgroup_high_fraction"] >= params["strong_subgroup_min_fraction"])
            & (
                result["subgroup_high_fraction_ratio"]
                >= params["strong_subgroup_min_fraction_ratio"]
            )
        )
    return strong_lineage, strong_subgroup


def confidence_tier(result, lineage, subgroup, params=PARAMS):
    """ "high" | "candidate" | None per pair; None exactly when the v3 pass fails.

    Read-only over the v3 arrays: a v3 pass is "high" if either arm meets its strong
    cutoffs (both inclusive), otherwise "candidate". The lineage arm retains its
    broad-expression block; the subgroup arm retains its base v3 gate and is tested
    regardless of pass_mode.
    """
    strong_lineage, strong_subgroup = strong_masks(result, lineage, subgroup, params)
    passed = lineage | subgroup
    high = passed & (strong_lineage | strong_subgroup)
    return np.where(high, "high", np.where(passed, "candidate", None))


def strong_arm(result, lineage, subgroup, params=PARAMS):
    """Which arm(s) made a high pair high; null whenever the pair is not high."""
    strong_lineage, strong_subgroup = strong_masks(result, lineage, subgroup, params)
    high = (lineage | subgroup) & (strong_lineage | strong_subgroup)
    arm = np.where(
        strong_lineage & strong_subgroup,
        "both",
        np.where(strong_lineage, "lineage", np.where(strong_subgroup, "subgroup", None)),
    )
    return np.where(high, arm, None)
