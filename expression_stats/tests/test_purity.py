"""R2 boundaries, sample identity, global BH and demotion-only behavior."""

import json

import numpy as np
import pytest

from gene_expression_relevance import PARAMS, purity


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "demoted"),
        ({"rho": -0.299999}, "pass"),
        ({"q": 0.05}, "pass"),
        ({"n": 29}, "unassessed"),
        ({"hp_n": 29}, "unassessed"),
        ({"hp_strong": True}, "pass"),
        ({"rho": None}, "unassessed"),
        *[({"code": code, "n": 0}, "not_applicable") for code in ["DLBC", "LAML", "THYM"]],
    ],
)
def test_r2_boundaries(changes, expected):
    args = dict(code="BRCA", rho=-0.30, q=0.049999, n=30, hp_n=30, hp_strong=False)
    assert purity.check(**(args | changes)) == expected


def test_purity_threshold_inclusive():
    assert purity.high_purity_mask(np.array([np.nan, 0.599999, 0.6, 1])).tolist() == [
        False,
        False,
        True,
        True,
    ]


@pytest.mark.parametrize("cached", [True, False])
def test_checksum_mismatch_fails_closed(tmp_path, monkeypatch, cached):
    target = tmp_path / "objects" / purity.SOURCE["sha256"]
    target.parent.mkdir()
    if cached:
        target.write_bytes(b"corrupt")
    monkeypatch.setattr(purity.http, "download", lambda url, path: path.write_bytes(b"corrupt"))
    with pytest.raises(ValueError, match="purity checksum mismatch"):
        purity.fetch(tmp_path)
    assert not target.exists()


def test_verified_cache_and_sample_join(tmp_path, monkeypatch):
    target = tmp_path / "objects" / purity.SOURCE["sha256"]
    target.parent.mkdir()
    target.write_bytes(b"verified")
    monkeypatch.setattr(purity, "digest", lambda path: purity.SOURCE["sha256"])
    assert purity.fetch(tmp_path) == target
    path = tmp_path / "purity.tsv"
    path.write_text(
        "sample\tcall status\tpurity\n"
        "TCGA-AA-0001-01A\tcalled\t0.6\n"
        "TCGA-AA-0001-02A\tcalled\t0.9\n"
        "TCGA-AA-0002-01A\tuncalled\t0.8\n"
        "TCGA-AA-0003-01A\tcalled\t1.1\n"
        "TCGA-AA-0004-01A\tcalled\tinf\n"
    )
    values = purity.load(path, [f"TCGA-AA-{i:04d}-01B" for i in range(1, 5)])
    assert values[0] == 0.6 and np.isnan(values[1:]).all()


def test_finalize_global_family_and_only_tier_mutates(tmp_path):
    records = [
        dict(oncotree_code=c, confidence_tier=t, stats_pass=True, log2fc=3)
        for c, t in [("BRCA", "high"), ("CHOL", "high"), ("DLBC", "high"), ("LGG", "candidate")]
    ]
    path = tmp_path / "G.json"
    doc = dict(gene=dict(hugo_symbol="G"), cancer_types=records)
    path.write_text(json.dumps(doc))
    tests = {
        ("G", c): dict(rho=-0.3, p=0.01, n=30, hp_n=30, hp_strong=False)
        for c in ["BRCA", "CHOL", "DLBC", "LGG"]
    }
    tests["G", "CHOL"]["hp_n"] = 29
    tests["G", "LGG"]["p"] = None  # unavailable still contributes p=1 to family
    counts = purity.finalize(tmp_path, tests)
    assert counts == dict(high_before=3, high_after=2, demoted=1)
    result = json.loads(path.read_text())["cancer_types"]
    assert [r["purity_check"] for r in result] == ["demoted", "unassessed", "not_applicable", None]
    assert result[0]["confidence_tier"] == "candidate"
    assert result[0]["purity_q"] == pytest.approx(0.04 / 3)
    for before, after in zip(records, result, strict=True):
        assert {
            k: v
            for k, v in after.items()
            if not k.startswith("purity_") and k not in ("confidence_tier", "strong_arm")
        } == {k: v for k, v in before.items() if k != "confidence_tier"}
        if after["purity_check"] != "demoted":
            assert before["confidence_tier"] == after["confidence_tier"]


def test_purity_demotes_a_newly_high_subgroup_pair(tmp_path):
    record = dict(
        oncotree_code="GBM",
        stats_pass=True,
        pass_mode="lineage",
        confidence_tier="high",
        strong_arm="subgroup",
        log2fc=2.73,
        cliffs_delta=0.58,
        subgroup_high_fraction=0.163,
        subgroup_high_fraction_ratio=21.7,
        p95_log2_diff=3.31,
    )
    path = tmp_path / "EGFR.json"
    path.write_text(json.dumps(dict(gene=dict(hugo_symbol="EGFR"), cancer_types=[record])))
    # The high-purity re-test no longer finds either arm strong.
    tests = {("EGFR", "GBM"): dict(rho=-0.55, p=0.001, n=40, hp_n=35, hp_strong=False)}
    assert purity.finalize(tmp_path, tests) == dict(high_before=1, high_after=0, demoted=1)
    result = json.loads(path.read_text())["cancer_types"][0]
    assert result["purity_check"] == "demoted"
    assert result["confidence_tier"] == "candidate" and result["strong_arm"] is None
    assert result["subgroup_high_fraction_ratio"] == pytest.approx(21.7)


def _measure_hp_strong(monkeypatch, original_arm, **overrides):
    raw = np.ones((1, 60))
    labels = np.array(["A"] * 30 + ["B"] * 30)
    original = dict(
        confidence_tier=np.array([["high", "candidate"]]),
        stats_pass=np.array([[True, True]]),
        broadly_expressed=np.array([False]),
        strong_arm=np.array([[original_arm, None]], dtype=object),
    )
    hp = {
        key: np.full((1, 2), value)
        for key, value in dict(
            n=30,
            n_rest=30,
            q_value=0.001,
            log2fc=1.5,
            cliffs_delta=0.8,
            median_log2=8,
            subgroup_high_count=10,
            subgroup_high_fraction=0.1,
            subgroup_high_fraction_ratio=6,
            p95_log2_diff=2,
            subgroup_q=0.001,
        ).items()
    }
    for key, value in overrides.items():
        hp[key][:] = value
    monkeypatch.setattr(purity, "compute", lambda *args: hp)
    return purity.measure(raw, labels, ["A", "B"], original, np.full(60, 0.6))[0, 0]["hp_strong"]


@pytest.mark.parametrize(
    "original_arm,overrides",
    [
        ("lineage", dict(log2fc=1.5, cliffs_delta=0.8)),
        (
            "subgroup",
            dict(
                log2fc=3.0,
                cliffs_delta=0.8,
                subgroup_high_fraction=0.03,
                subgroup_high_fraction_ratio=3.0,
            ),
        ),
    ],
    ids=["lineage", "subgroup"],
)
def test_purity_retests_only_the_original_strong_arm(monkeypatch, original_arm, overrides):
    assert _measure_hp_strong(monkeypatch, original_arm, **overrides) is False


@pytest.mark.parametrize(
    "overrides",
    [
        dict(log2fc=3.0, subgroup_high_fraction=0.03, subgroup_high_fraction_ratio=3.0),
        dict(log2fc=1.5),
    ],
    ids=["lineage", "subgroup"],
)
def test_purity_both_original_arms_use_either_filtered_arm(monkeypatch, overrides):
    assert _measure_hp_strong(monkeypatch, "both", **overrides) is True


def test_frozen_broad_and_lineage_priority(monkeypatch):
    raw = np.ones((1, 60))
    labels = np.array(["A"] * 30 + ["B"] * 30)
    original = dict(
        confidence_tier=np.array([["high", "candidate"]]),
        stats_pass=np.array([[True, True]]),
        broadly_expressed=np.array([False]),
        strong_arm=np.array([["subgroup", None]], dtype=object),
    )
    hp = {
        key: np.full((1, 2), value)
        for key, value in dict(
            n=30,
            n_rest=30,
            q_value=0.001,
            log2fc=1.5,
            cliffs_delta=0.8,
            median_log2=8,
            subgroup_high_count=10,
            subgroup_high_fraction=0.1,
            subgroup_high_fraction_ratio=6,
            p95_log2_diff=2,
            subgroup_q=0.001,
        ).items()
    }
    monkeypatch.setattr(purity, "compute", lambda *args: hp)
    result = purity.measure(raw, labels, ["A", "B"], original, np.full(60, 0.6))
    # Lineage passes v3 but fails its strong FC; the strong subgroup tail rescues it.
    assert result[0, 0]["hp_strong"] is True
    # A broad-blocked lineage arm still leaves the subgroup arm to retain high.
    original["broadly_expressed"][:] = True
    result = purity.measure(raw, labels, ["A", "B"], original, np.full(60, 0.6))
    assert result[0, 0]["hp_strong"] is True
    # Without either strong arm the re-test cannot retain the high tier.
    hp["subgroup_high_fraction"][:] = 0.03
    hp["subgroup_high_fraction_ratio"][:] = 3.0
    result = purity.measure(raw, labels, ["A", "B"], original, np.full(60, 0.6))
    assert result[0, 0]["hp_strong"] is False
    assert PARAMS["purity_min_target"] == 30


@pytest.mark.parametrize("key", [key for key in PARAMS if key.startswith("purity_")])
def test_every_purity_parameter_changes_release_identity(key):
    from gene_expression_relevance.provenance import build_fingerprint

    def identity(params):
        return build_fingerprint(params, None, True, "hgnc", "code", "mapping")

    changed = dict(PARAMS)
    changed[key] = ["OTHER"] if isinstance(PARAMS[key], list) else PARAMS[key] + 0.01
    assert identity(changed) != identity(PARAMS)


@pytest.mark.parametrize("key", ["url", "sha256"])
def test_purity_source_pin_changes_release_identity(monkeypatch, key):
    from gene_expression_relevance.provenance import build_fingerprint

    before = build_fingerprint(PARAMS, None, True, "hgnc", "code", "mapping")
    monkeypatch.setitem(purity.SOURCE, key, purity.SOURCE[key] + "changed")
    assert build_fingerprint(PARAMS, None, True, "hgnc", "code", "mapping") != before


def test_measure_real_statistics_and_global_bh(tmp_path):
    from scipy.stats import spearmanr

    from gene_expression_relevance.stats import compute

    codes = ["A", "B"]
    labels = np.repeat(codes, 80)
    values = np.concatenate([np.linspace(0.2, 0.9, 80), np.full(80, 0.8)])
    log_expression = np.array(
        [
            [12] * 40 + [5] * 40 + [4] * 80,
            [10] * 80 + [4] * 80,  # constant expression in each cohort
            [4] * 80 + list(np.linspace(9, 11, 80)),  # constant purity in cohort B
            [5.5] * 80 + [4] * 80,  # v3 candidate still belongs in the BH family
        ],
        dtype=float,
    )
    raw = np.exp2(log_expression) - 1
    raw[0, [0, 79]] = np.nan
    values[1] = np.nan
    original = compute(raw, labels, codes)
    assert original["stats_pass"].tolist() == [
        [True, False],
        [True, False],
        [False, True],
        [True, False],
    ]
    assert original["confidence_tier"][:, 0].tolist() == ["high", "high", None, "candidate"]
    measured = purity.measure(raw, labels, codes, original, values)
    assert set(measured) == {(0, 0), (1, 0), (2, 1), (3, 0)}
    matched = (labels == "A") & np.isfinite(values) & np.isfinite(raw[0])
    expected_rho, expected_p = spearmanr(np.log2(raw[0, matched] + 1), values[matched])
    falling = measured[0, 0]
    assert falling["n"] == 77
    assert falling["rho"] == pytest.approx(expected_rho)
    assert falling["rho"] < -0.8
    assert falling["p"] == pytest.approx(expected_p, abs=0, rel=1e-12)
    assert falling["hp_n"] == 33 and falling["hp_strong"] is False
    for key in [(1, 0), (2, 1), (3, 0)]:
        assert measured[key]["rho"] is None and measured[key]["p"] is None
    assert measured[1, 0]["n"] == 79  # missing purity, finite expression
    assert measured[2, 1]["n"] == 80  # constant purity still counts matched samples

    tests = {}
    for i in range(len(raw)):
        rows = []
        for j, code in enumerate(codes):
            rows.append(dict(oncotree_code=code, confidence_tier=original["confidence_tier"][i, j]))
            if (i, j) in measured:
                tests[str(i), code] = measured[i, j]
        (tmp_path / f"{i}.json").write_text(
            json.dumps(dict(gene=dict(hugo_symbol=str(i)), cancer_types=rows))
        )
    assert purity.finalize(tmp_path, tests) == dict(high_before=3, high_after=2, demoted=1)
    result = json.loads((tmp_path / "0.json").read_text())["cancer_types"][0]
    assert result["purity_check"] == "demoted" and result["confidence_tier"] == "candidate"
    # One testable p, two untestable highs, and an untestable candidate: q = p * 4.
    assert result["purity_q"] == pytest.approx(expected_p * 4, abs=0, rel=1e-12)
    assert all(test["q"] == 1 for key, test in tests.items() if key != ("0", "A"))


def test_purity_alignment_matches_selected_matrix_order(tmp_path, monkeypatch):
    from gene_expression_relevance.build import matrix, select_samples

    # Both study insertion order and sample columns disagree with cohort/primary order.
    mapping = [dict(study_id="a", oncotree_code="A"), dict(study_id="b", oncotree_code="B")]
    paths = {}
    for study, samples, expression in [
        ("b", ["TCGA-BB-0002-01A", "TCGA-BB-0001-01A"], [42, 41]),
        (
            "a",
            ["TCGA-AA-0002-01B", "TCGA-AA-0001-01A", "TCGA-AA-0002-01A", "TCGA-AA-0003-02A"],
            [999, 11, 12, 999],
        ),
    ]:
        path = tmp_path / f"{study}.tsv"
        path.write_text(
            "Hugo_Symbol\tEntrez_Gene_Id\t"
            + "\t".join(samples)
            + "\nG\t1\t"
            + "\t".join(map(str, expression))
            + "\n"
        )
        paths[study] = path
    table = tmp_path / "purity.tsv"
    table.write_text(
        "sample\tcall status\tpurity\n"
        "TCGA-BB-0002-01A\tcalled\t0.42\n"
        "TCGA-AA-0002-01A\tcalled\t0.12\n"
        "TCGA-AA-0003-02A\tcalled\t0.99\n"
        "TCGA-BB-0001-01A\tcalled\t0.41\n"
        "TCGA-AA-0001-01A\tcalled\t0.11\n"
    )
    monkeypatch.setattr(purity, "fetch", lambda cache: table)
    selection = select_samples(paths, mapping)
    assert selection["a"] == ["TCGA-AA-0001-01A", "TCGA-AA-0002-01A"]
    expression, labels, ids = matrix(paths, mapping, {1: "G"}, tmp_path / "matrix", selection)
    aligned = purity.load_selected(tmp_path, mapping, selection)
    assert ids == [1] and labels.tolist() == ["A", "A", "B", "B"]
    assert expression[0].tolist() == [11, 12, 41, 42]
    np.testing.assert_allclose(aligned, expression[0] / 100)
