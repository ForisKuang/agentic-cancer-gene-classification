"""Tests for accepting singleton genes alongside fusion inputs."""

import pytest

from src.models.schema import AnnotateRequest, FusionInput, GeneAnnotation, ResolvedGene
from src.pipeline.normalization import is_fusion_input
from src.pipeline.orchestrator import run_pipeline


def test_is_fusion_input_requires_two_partners():
    assert is_fusion_input("EML4::ALK")
    assert is_fusion_input("BCR/ABL1")
    assert not is_fusion_input("ALK")
    assert not is_fusion_input("ALK::")


def test_annotate_request_accepts_single_gene_strings_and_aliases():
    request = AnnotateRequest(
        fusions=[
            "ALK",
            {"gene": "BRAF", "tumor_type": "melanoma"},
            {"input": "EML4::ALK"},
            {"query": "BCR::ABL1"},
        ]
    )

    assert [item.fusion for item in request.fusions] == [
        "ALK",
        "BRAF",
        "EML4::ALK",
        "BCR::ABL1",
    ]
    assert request.fusions[1].tumor_type == "melanoma"


@pytest.mark.asyncio
async def test_run_pipeline_passes_single_gene_without_associated_fusion(monkeypatch):
    seen = {}

    async def fake_normalize_fusions(inputs):
        seen["inputs"] = inputs
        return {
            "ALK": (
                ResolvedGene(
                    input_symbol="ALK",
                    canonical_symbol="ALK",
                    resolved=True,
                ),
                ["ALK"],
            )
        }

    async def fake_annotate_gene(
        gene,
        fusions,
        resolved_gene,
        unresolvable,
        tumor_type=None,
        local_mode=False,
        local_backend=None,
    ):
        seen["annotation"] = {
            "gene": gene,
            "fusions": fusions,
            "tumor_type": tumor_type,
            "local_mode": local_mode,
            "local_backend": local_backend,
        }
        return GeneAnnotation(gene=gene, fusions=fusions)

    monkeypatch.setattr("src.pipeline.orchestrator.normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr("src.pipeline.orchestrator._annotate_gene", fake_annotate_gene)

    result = await run_pipeline([FusionInput(gene="ALK", tumor_type="LUAD")])

    assert seen["inputs"] == ["ALK"]
    assert seen["annotation"] == {
        "gene": "ALK",
        "fusions": [],
        "tumor_type": "LUAD",
        "local_mode": False,
        "local_backend": None,
    }
    assert result.fusions_processed == 1
    assert result.annotations[0].fusions == []


@pytest.mark.asyncio
async def test_run_pipeline_preserves_real_fusions_for_partner_context(monkeypatch):
    seen = {}

    async def fake_normalize_fusions(inputs):
        seen["inputs"] = inputs
        return {
            "ALK": (
                ResolvedGene(
                    input_symbol="ALK",
                    canonical_symbol="ALK",
                    resolved=True,
                ),
                ["ALK", "EML4::ALK"],
            )
        }

    async def fake_annotate_gene(
        gene,
        fusions,
        resolved_gene,
        unresolvable,
        tumor_type=None,
        local_mode=False,
        local_backend=None,
    ):
        seen["annotation"] = {
            "gene": gene,
            "fusions": fusions,
            "tumor_type": tumor_type,
        }
        return GeneAnnotation(gene=gene, fusions=fusions)

    monkeypatch.setattr("src.pipeline.orchestrator.normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr("src.pipeline.orchestrator._annotate_gene", fake_annotate_gene)

    result = await run_pipeline(
        [
            FusionInput(gene="ALK", tumor_type="LUAD"),
            FusionInput(fusion="EML4::ALK", tumor_type="lung adenocarcinoma"),
        ]
    )

    assert seen["inputs"] == ["ALK", "EML4::ALK"]
    assert seen["annotation"] == {
        "gene": "ALK",
        "fusions": ["EML4::ALK"],
        "tumor_type": "LUAD",
    }
    assert result.fusions_processed == 2
    assert result.annotations[0].fusions == ["EML4::ALK"]
