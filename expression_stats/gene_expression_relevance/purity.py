"""Pinned ABSOLUTE purity and the R2 demotion-only sensitivity gate."""

import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from . import PARAMS, http
from .fetch import digest
from .stats import bh, compute, passes, strong_masks, subgroup_pass

SOURCE = dict(
    url="https://api.gdc.cancer.gov/data/4f277128-f793-4354-a13d-30cc7fe9f6b5",
    sha256="f430a975433d82e0098d7405619d4f12a0c765fcd97e7d63cc9b1de7f2d763cd",
    version="TCGA_mastercalls.abs_tables_JSedit.fixed.txt",
)


def fetch(cache):
    """Verify cached and freshly downloaded bytes; any mismatch fails the build."""
    target = Path(cache) / "objects" / SOURCE["sha256"]
    if not target.exists():
        http.download(SOURCE["url"], target)
    if digest(target) != SOURCE["sha256"]:
        target.unlink()
        raise ValueError("ABSOLUTE purity checksum mismatch")
    return target


def load(path, samples):
    frame = pd.read_csv(path, sep="\t")
    frame["purity"] = pd.to_numeric(frame["purity"], errors="coerce")
    frame = frame[frame["call status"].eq("called") & frame.purity.between(0, 1)].copy()
    frame["key"] = frame["sample"].str[:15]
    if frame.key.duplicated().any():
        raise ValueError("Duplicate called ABSOLUTE sample keys")
    lookup = frame.set_index("key").purity
    return np.asarray([lookup.get(sample[:15], np.nan) for sample in samples])


def load_selected(cache, mapping, selection):
    return load(fetch(cache), [s for item in mapping for s in selection[item["study_id"]]])


def high_purity_mask(purity, params=PARAMS):
    return np.isfinite(purity) & (purity >= params["purity_min"])


def check(code, rho, q, n, hp_n, hp_strong, params=PARAMS):
    """Call only for pre-purity high pairs. Missing coverage never means pass."""
    if code in params["purity_exempt_cohorts"]:
        return "not_applicable"
    if n < params["purity_min_matched"] or hp_n < params["purity_min_target"]:
        return "unassessed"
    if rho is None or q is None:
        return "unassessed"
    if rho <= params["purity_rho_max"] and q < params["purity_fdr"] and not hp_strong:
        return "demoted"
    return "pass"


def measure(raw, labels, codes, original, purity, params=PARAMS):
    """All v3 passes enter one global BH family, including unavailable tests (p=1).

    Recompute the filtered statistics across every cohort for each high gene, then
    restore the original broad flag and lineage-before-subgroup priority. Only an arm
    that made the pair high before filtering can retain it after filtering.
    """
    tests = {}
    high = high_purity_mask(purity, params)
    needed = np.flatnonzero(np.any(original["confidence_tier"] == "high", axis=1))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        hp = compute(raw[needed][:, high], labels[high], codes, params) if len(needed) else None
    hp_rows = {row: i for i, row in enumerate(needed)}
    if hp is not None:
        lineage = (
            passes(
                hp["q_value"], hp["log2fc"], hp["cliffs_delta"], hp["median_log2"], hp["n"], params
            )
            & ~original["broadly_expressed"][needed, None]
        )
        subgroup = subgroup_pass(hp, params)
        strong_lineage, strong_subgroup = strong_masks(hp, lineage, subgroup, params)
        original_arm = original["strong_arm"][needed]
        hp_strong = (
            ((original_arm == "lineage") & strong_lineage)
            | ((original_arm == "subgroup") & strong_subgroup)
            | ((original_arm == "both") & (strong_lineage | strong_subgroup))
        )
    for i, j in zip(*np.where(original["stats_pass"]), strict=True):
        matched = (labels == codes[j]) & np.isfinite(purity) & np.isfinite(raw[i])
        z, v = np.log2(raw[i, matched] + 1), purity[matched]
        n = int(np.isfinite(z).sum())
        finite = np.isfinite(z)
        rho = p = None
        if (
            n >= params["purity_correlation_min_n"]
            and np.ptp(v[finite]) > 0
            and np.ptp(z[finite]) > 0
        ):
            rho, p = (float(value) for value in spearmanr(z[finite], v[finite]))
        h = hp_rows.get(i)
        tests[i, j] = dict(
            rho=rho,
            p=p,
            n=n,
            hp_n=int(hp["n"][h, j]) if h is not None else 0,
            hp_strong=bool(hp_strong[h, j]) if h is not None else False,
        )
    return tests


def finalize(genes_dir, tests, params=PARAMS):
    """Apply the global q values after every batch, before controls/publication."""
    keys = list(tests)
    qs = bh(np.asarray([tests[key]["p"] if tests[key]["p"] is not None else 1 for key in keys]))
    for key, q in zip(keys, qs, strict=True):
        tests[key]["q"] = float(q)
    counts = dict(high_before=0, high_after=0, demoted=0)
    for path in sorted(Path(genes_dir).glob("*.json")):
        artifact = json.loads(path.read_text())
        symbol = artifact["gene"]["hugo_symbol"]
        for row in artifact["cancer_types"]:
            test = tests.get((symbol, row["oncotree_code"]))
            row.update(purity_check=None, purity_rho=None, purity_q=None)
            if test:
                row.update(purity_rho=test["rho"], purity_q=test["q"])
            if row["confidence_tier"] == "high":
                counts["high_before"] += 1
                row["purity_check"] = check(
                    row["oncotree_code"],
                    test["rho"],
                    test["q"],
                    test["n"],
                    test["hp_n"],
                    test["hp_strong"],
                    params,
                )
                if row["purity_check"] == "demoted":
                    row["confidence_tier"] = "candidate"
                    row["strong_arm"] = None
                    counts["demoted"] += 1
                else:
                    counts["high_after"] += 1
        path.write_text(json.dumps(artifact, allow_nan=False, separators=(",", ":")) + "\n")
    return counts
