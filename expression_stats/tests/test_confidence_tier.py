"""Confidence tier (contract 1.3): read-only over the unchanged v3 pass."""

import json
from pathlib import Path

import numpy as np
import pytest
from test_bundle import CODES as BUNDLE_CODES
from test_bundle import PANEL, bundled, load, rebundle  # noqa: F401 (fixture)

from gene_expression_relevance import CONTRACT_VERSION, METHOD_VERSION, PARAMS, controls
from gene_expression_relevance.stats import compute, confidence_tier, subgroup_pass

CODES = [f"T{i:02d}" for i in range(32)]


def arrays(**values):
    """One pair (1x1 arrays) with the v3 fields confidence_tier reads."""
    base = dict(
        log2fc=1.0,
        cliffs_delta=0.5,
        n=100.0,
        n_rest=1000.0,
        subgroup_high_count=10.0,
        subgroup_high_fraction=0.03,
        subgroup_high_fraction_ratio=3.0,
        p95_log2_diff=2.0,
        subgroup_q=0.01,
    )
    base.update(values)
    return {key: np.array([[value]], dtype=float) for key, value in base.items()}


def tier(lineage=False, subgroup=False, **values):
    return confidence_tier(arrays(**values), np.array([[lineage]]), np.array([[subgroup]]), PARAMS)[
        0, 0
    ]


def test_cutoff_values_and_contract_version():
    assert (
        PARAMS["strong_log2fc"],
        PARAMS["strong_cliffs_delta"],
        PARAMS["strong_subgroup_min_fraction"],
        PARAMS["strong_subgroup_min_fraction_ratio"],
    ) == (2.0, 0.70, 0.05, 5.0)
    # v3 thresholds are untouched; only the contract version moves.
    assert (PARAMS["log2fc"], PARAMS["cliffs_delta"]) == (1.0, 0.5)
    assert (PARAMS["subgroup_min_fraction"], PARAMS["subgroup_min_fraction_ratio"]) == (0.03, 3.0)
    assert METHOD_VERSION == "3" and CONTRACT_VERSION == "1.5"


@pytest.mark.parametrize(
    "cutoff",
    [
        "strong_log2fc",
        "strong_cliffs_delta",
        "strong_subgroup_min_fraction",
        "strong_subgroup_min_fraction_ratio",
    ],
)
def test_changing_a_strong_cutoff_changes_the_release_id(tmp_path, monkeypatch, cutoff):
    """End to end through build(): the cutoffs are hashed method params."""
    from test_contract import test_build_contract_and_precheck_gating

    from gene_expression_relevance import build as builder
    from gene_expression_relevance.controls import evaluate

    panel = {"controls": [dict(id="ALB", gene="ALB", tcga="LIHC", expected=True)]}
    monkeypatch.setattr(
        builder, "evaluate_controls", lambda directory, **kw: evaluate(directory, panel=panel, **kw)
    )
    # Runs the fixture build; it finishes with an ALB-only subset build into site/.
    test_build_contract_and_precheck_gating(tmp_path, monkeypatch, True, None)
    out, cache = tmp_path / "site", tmp_path / "cache"

    def release_id():
        builder.build("a" * 40, out, cache, genes=["ALB"], skip_precheck=True)
        return json.loads((out / "v1/manifest.json").read_text())["release_id"]

    baseline = release_id()
    assert release_id() == baseline  # deterministic for identical inputs
    monkeypatch.setitem(PARAMS, cutoff, PARAMS[cutoff] * 1.1)
    changed = release_id()
    assert changed != baseline
    assert changed.rsplit("-", 1)[0] == baseline.rsplit("-", 1)[0]  # same commit and m3
    manifest = json.loads((out / "v1/manifest.json").read_text())
    assert manifest["params"][cutoff] == PARAMS[cutoff]


@pytest.mark.parametrize(
    "fc,delta,expected",
    [
        (2.0, 0.70, "high"),  # both cutoffs inclusive
        (5.0, 0.95, "high"),
        (np.nextafter(2.0, 0), 0.70, "candidate"),
        (2.0, np.nextafter(0.70, 0), "candidate"),
        (1.0, 0.5, "candidate"),  # the v3 minimum
    ],
)
def test_lineage_arm_boundaries(fc, delta, expected):
    assert tier(lineage=True, log2fc=fc, cliffs_delta=delta) == expected
    # Strong subgroup statistics do not promote a lineage-first pass unless the base
    # subgroup gate also passes (subgroup=True here means that gate passed).
    assert (
        tier(lineage=True, log2fc=fc, cliffs_delta=delta, subgroup_high_fraction=0.5,
             subgroup_high_fraction_ratio=50.0)
        == expected
    )  # fmt: skip


@pytest.mark.parametrize(
    "fraction,ratio,expected",
    [
        (0.05, 5.0, "high"),
        (0.20, 40.0, "high"),
        (np.nextafter(0.05, 0), 5.0, "candidate"),
        (0.05, np.nextafter(5.0, 0), "candidate"),
        (0.03, 3.0, "candidate"),
    ],
)
def test_subgroup_arm_boundaries(fraction, ratio, expected):
    values = dict(subgroup_high_fraction=fraction, subgroup_high_fraction_ratio=ratio)
    assert tier(subgroup=True, **values) == expected
    # The lineage strong mask is gated on the lineage arm itself (incl. its broad block),
    # so lineage statistics alone cannot promote a subgroup-only pass.
    assert tier(subgroup=True, log2fc=4.0, cliffs_delta=0.9, **values) == expected


def test_either_arm_can_make_a_pass_high():
    # EGFR/GBM in the full build: log2FC 2.73, delta 0.58, 16% high, 21.7x. Lineage
    # passes but misses its strong delta; the subgroup tail promotes it only after its
    # base gate passes.
    weak_lineage = dict(log2fc=2.73, cliffs_delta=0.58)
    strong_subgroup = dict(subgroup_high_fraction=0.16, subgroup_high_fraction_ratio=21.7)
    assert tier(lineage=True, **weak_lineage, **strong_subgroup) == "candidate"
    # pass_mode stays lineage even when subgroup also passes: the subgroup arm still counts.
    assert tier(lineage=True, subgroup=True, **weak_lineage, **strong_subgroup) == "high"
    assert tier(lineage=True, **weak_lineage) == "candidate"
    strong_lineage = dict(log2fc=2.0, cliffs_delta=0.70)
    assert tier(lineage=True, subgroup=True, **strong_lineage) == "high"


@pytest.mark.parametrize(
    "failure",
    [dict(subgroup_q=0.025), dict(p95_log2_diff=np.nextafter(1.5, 0))],
    ids=["subgroup_q", "p95_log2_diff"],
)
def test_strong_subgroup_requires_the_base_v3_gate(failure):
    weak_lineage = dict(log2fc=2.73, cliffs_delta=0.58)
    strong_tail = dict(subgroup_high_fraction=0.16, subgroup_high_fraction_ratio=21.7)
    values = dict(
        n=100.0, n_rest=1000.0, subgroup_high_count=16.0, **strong_tail, **failure
    )
    assert values["subgroup_high_fraction"] >= PARAMS["strong_subgroup_min_fraction"]
    assert values["subgroup_high_fraction_ratio"] >= PARAMS["strong_subgroup_min_fraction_ratio"]
    result = arrays(**weak_lineage, **values)
    subgroup = subgroup_pass(result, PARAMS)
    assert not subgroup[0, 0]
    assert confidence_tier(result, np.array([[True]]), subgroup, PARAMS)[0, 0] == "candidate"

    passing = arrays(
        **weak_lineage,
        **dict(values, subgroup_q=0.024999, p95_log2_diff=1.5),
    )
    subgroup = subgroup_pass(passing, PARAMS)
    assert subgroup[0, 0]
    assert confidence_tier(passing, np.array([[True]]), subgroup, PARAMS)[0, 0] == "high"


def test_strong_arm_records_the_promoting_arm():
    from gene_expression_relevance.stats import strong_arm

    weak_lineage = dict(log2fc=2.73, cliffs_delta=0.58)
    strong_subgroup = dict(subgroup_high_fraction=0.16, subgroup_high_fraction_ratio=21.7)
    strong_lineage = dict(log2fc=2.0, cliffs_delta=0.70)
    weak_subgroup = dict(subgroup_high_fraction=0.03, subgroup_high_fraction_ratio=3.0)

    def arm(lineage=False, subgroup=False, **values):
        return strong_arm(
            arrays(**values), np.array([[lineage]]), np.array([[subgroup]]), PARAMS
        )[0, 0]

    assert arm(lineage=True, **weak_lineage, **strong_subgroup) is None
    assert arm(lineage=True, subgroup=True, **weak_lineage, **strong_subgroup) == "subgroup"
    # A broad-blocked lineage arm leaves only the subgroup strong mask.
    assert arm(subgroup=True, **weak_lineage, **strong_subgroup) == "subgroup"
    assert arm(lineage=True, **strong_lineage) == "lineage"
    assert arm(lineage=True, subgroup=True, **strong_lineage, **strong_subgroup) == "both"
    assert arm(lineage=True, **weak_lineage, **weak_subgroup) is None
    assert arm(**strong_lineage, **strong_subgroup) is None  # no v3 pass, no high


def test_failing_v3_is_null_whatever_the_statistics():
    strong = dict(log2fc=9.0, cliffs_delta=1.0, subgroup_high_fraction=1.0)
    assert tier(**strong, subgroup_high_fraction_ratio=99.0) is None
    assert tier(lineage=True, log2fc=np.nan, cliffs_delta=np.nan) == "candidate"


def fixture():
    """Seeded synthetic matrix with lineage, subgroup, NaN and zero patterns."""
    rng = np.random.default_rng(20261113)
    sizes = rng.integers(8, 60, size=32)
    labels = np.repeat(CODES, sizes)
    genes = 48
    x = rng.lognormal(mean=3.0, sigma=1.2, size=(genes, len(labels)))
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    for g in range(genes):
        t = g % 32
        s, n = starts[t], sizes[t]
        x[g, s : s + n] *= 2 ** (g % 7)
        if g % 5 == 0:
            k = max(1, n // (3 + g % 4))
            x[g, s : s + k] *= 400
        if g % 6 == 0:
            x[g, rng.integers(0, len(labels), 20)] = np.nan
        if g % 9 == 0:
            x[g, rng.integers(0, len(labels), 50)] = 0.0
    x[47, :] = 31.0
    return x, labels


def test_v3_fields_match_contract_1_2():
    # Captured from contract 1.2 (1015fe4) for fixture(); its serialized SHA-256
    # was 6952563ec74c75915dc68f6828ae76b55f71a9b0bed1443a7fe493110d8b5784.
    expected = json.loads((Path(__file__).parent / "fixtures/contract-1.2-stats.json").read_text())
    x, labels = fixture()
    result = compute(x, labels, CODES)
    assert set(result) - {"confidence_tier", "strong_arm"} == set(expected)
    exact = {
        "n",
        "n_rest",
        "subgroup_high_count",
        "broadly_expressed",
        "lineage_blocked_by_broad",
        "stats_pass",
        "pass_mode",
    }
    for key in sorted(expected):
        if key in exact:
            np.testing.assert_array_equal(result[key], expected[key], err_msg=key)
        else:
            # Rounding in transcendental functions varies slightly across CPU/libm;
            # this is far below any method cutoff, while a meaningful change fails.
            np.testing.assert_allclose(
                result[key],
                np.asarray(expected[key], dtype=float),
                rtol=1e-9,
                atol=1e-12,
                equal_nan=True,
                err_msg=key,
            )
    tiers = result["confidence_tier"]
    np.testing.assert_array_equal(tiers != None, result["stats_pass"])  # noqa: E711
    assert set(tiers[result["stats_pass"]]) <= {"high", "candidate"}
    # The fixture exercises both tiers.
    assert {"high", "candidate"} <= set(tiers[result["stats_pass"]])
    # strong_arm is populated exactly for high pairs.
    np.testing.assert_array_equal(result["strong_arm"] != None, tiers == "high")  # noqa: E711
    assert set(result["strong_arm"][tiers == "high"]) <= {"lineage", "subgroup", "both"}


def control_documents(tmp_path, tiers):
    """Fixture control documents (controls-results.json) with per-pair tiers."""
    rows = json.loads((Path(__file__).parent / "fixtures/controls-results.json").read_text())
    for gene in {r["gene"] for r in rows}:
        records = []
        for r in rows:
            if r["gene"] == gene:
                tier = tiers.get((gene, r["tcga"]), "high") if r["stats_pass"] else None
                records.append(
                    dict(oncotree_code=r["tcga"], stats_pass=r["stats_pass"], confidence_tier=tier)
                )
        (tmp_path / f"{gene}.json").write_text(json.dumps(dict(cancer_types=records)))
    return tmp_path


EXPECTED_CANDIDATES = {
    ("PAX8", "KIRC"),
    ("ERBB2", "BRCA"),
    ("ERBB2", "STAD"),
    ("EGFR", "GBM"),
    ("MSLN", "PAAD"),
    ("SLC34A2", "OV"),
}


def test_controls_report_tier_breakdown_without_gating_positives(tmp_path):
    genes = control_documents(tmp_path, dict.fromkeys(EXPECTED_CANDIDATES, "candidate"))
    report = controls.evaluate(genes)
    controls.require_passed(report)  # candidate positives still pass the v3 gate
    tiers = report["tiers"]
    assert (tiers["positive_high"], tiers["positive_candidate"], tiers["positive_total"]) == (
        45,
        6,
        51,
    )
    assert (tiers["negative_high"], tiers["negative_total"]) == (0, 27)
    by_id = {r["id"]: r for r in report["controls"]}
    assert {(by_id[i]["gene"], by_id[i]["tcga"]) for i in tiers["positive_candidate_ids"]} == (
        EXPECTED_CANDIDATES
    )
    assert all("confidence_tier" in row for row in report["controls"])
    assert controls.summary(report)["tiers"] == tiers


def test_a_high_negative_fails_the_gate(tmp_path):
    genes = control_documents(tmp_path, {})
    path = genes / "GAPDH.json"
    document = json.loads(path.read_text())
    document["cancer_types"][0]["confidence_tier"] = "high"  # stats_pass stays false
    path.write_text(json.dumps(document))
    report = controls.evaluate(genes)
    assert report["status"] == "failed" and report["tiers"]["negative_high"] == 1
    assert report["negative_passed"] == 0
    with pytest.raises(ValueError, match="Controls gate"):
        controls.require_passed(report)


def make_negative_high(genes_dir):
    negative = next(r for r in PANEL["controls"] if r["expected"] is False)
    path = genes_dir / f"{negative['gene']}.json"
    document = json.loads(path.read_text())
    for record in document["cancer_types"]:
        if record["oncotree_code"] == negative["tcga"]:
            record["confidence_tier"] = "high"
    path.write_text(json.dumps(document) + "\n")


def make_positive_candidate(genes_dir):
    path = genes_dir / "ERBB2.json"
    document = json.loads(path.read_text())
    for record in document["cancer_types"]:
        if record["oncotree_code"] == "BRCA":
            record["confidence_tier"] = "candidate"
    path.write_text(json.dumps(document) + "\n")


@pytest.mark.parametrize(
    "case", [None, "positive_candidate", "negative_high_report", "negative_high_bundle"]
)
def test_publisher_blocks_any_high_negative_but_not_candidate_positives(
    bundled,  # noqa: F811
    monkeypatch,
    tmp_path,
    case,
):
    module, _, _, manifest, _ = bundled
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    publisher = load("publish")
    edit = {
        None: None,
        "positive_candidate": make_positive_candidate,
        "negative_high_report": None,
        # Tampered after the gate: only the publish-time recheck can see it.
        "negative_high_bundle": make_negative_high,
    }[case]
    directory, manifest, report = rebundle(module, tmp_path, PANEL, manifest, edit)
    if case in ("positive_candidate", "negative_high_report"):
        if case == "positive_candidate":
            # A consistent report for a bundle whose positive is only a candidate.
            report = controls.evaluate(
                tmp_path / "custom/v1/genes", panel=PANEL, codes=BUNDLE_CODES, alterations=True
            )
            assert report["tiers"]["positive_candidate"] == 1
        else:
            report["tiers"]["negative_high"] = 1
        manifest["controls"] = {k: v for k, v in report.items() if k != "controls"}
        (directory / "manifest.json").write_text(json.dumps(manifest))
        (directory / "controls-report.json").write_text(json.dumps(report))
    calls = []

    def gh(*args):
        calls.append(args)
        return json.dumps([[dict(tag_name=manifest["bundle_tag"], draft=False)]])

    monkeypatch.setattr(publisher, "gh", gh)
    if case in (None, "positive_candidate"):
        publisher.publish(directory)
        assert len(calls) == 1  # reached published-tag discovery
    else:
        with pytest.raises(ValueError, match="controls report|control recheck"):
            publisher.publish(directory)
        assert calls == []
