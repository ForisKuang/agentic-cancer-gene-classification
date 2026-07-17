"""Tests for persistent LLM evidence extraction caching."""

from __future__ import annotations

import pytest

from src.models.schema import LiteratureRecord
from src.pipeline import synthesis


@pytest.mark.asyncio
async def test_extract_paper_evidence_reuses_persistent_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(synthesis.settings, "llm_cache_enabled", True)

    calls = 0

    async def fake_complete_with_tool(**kwargs):
        nonlocal calls
        calls += 1
        return {
            "relevance": "direct",
            "evidence_types": ["functional assay"],
            "key_findings": ["TP53 loss promotes cancer growth."],
            "cancer_types": ["solid tumor"],
            "caveats": [],
        }

    monkeypatch.setattr(synthesis, "complete_with_tool", fake_complete_with_tool)

    record = LiteratureRecord(
        pmid="12345",
        title="TP53 functional evidence",
        abstract="TP53 loss promotes cancer growth in a functional assay.",
    )

    first = await synthesis._extract_paper_evidence(  # noqa: SLF001
        "TP53",
        record,
        "HGNC name: tumor protein p53",
        local_mode=False,
        local_backend=None,
    )
    second = await synthesis._extract_paper_evidence(  # noqa: SLF001
        "TP53",
        record,
        "HGNC name: tumor protein p53",
        local_mode=False,
        local_backend=None,
    )

    assert first == second
    assert calls == 1


@pytest.mark.asyncio
async def test_extract_evidence_packets_batches_missing_records(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(synthesis.settings, "llm_cache_enabled", True)
    monkeypatch.setattr(synthesis.settings, "evidence_extraction_batch_size", 4)

    calls = 0

    async def fake_complete_with_tool(**kwargs):
        nonlocal calls
        calls += 1
        return {
            "papers": [
                {
                    "pmid": pmid,
                    "relevance": "direct",
                    "evidence_types": ["functional assay"],
                    "key_findings": [f"Finding for PMID {pmid}."],
                    "cancer_types": ["solid tumor"],
                    "caveats": [],
                }
                for pmid in ("1", "2", "3")
            ]
        }

    monkeypatch.setattr(synthesis, "complete_with_tool", fake_complete_with_tool)

    records = [
        LiteratureRecord(pmid=str(i), title=f"Paper {i}", abstract=f"GENE cancer evidence {i}.")
        for i in range(1, 4)
    ]

    first = await synthesis._extract_evidence_packets(  # noqa: SLF001
        "GENE",
        records,
        gene_identity=None,
        local_mode=False,
        local_backend=None,
    )
    second = await synthesis._extract_evidence_packets(  # noqa: SLF001
        "GENE",
        records,
        gene_identity=None,
        local_mode=False,
        local_backend=None,
    )

    assert [packet["record"].pmid for packet in first] == ["1", "2", "3"]
    assert [packet["evidence"]["key_findings"][0] for packet in second] == [
        "Finding for PMID 1.",
        "Finding for PMID 2.",
        "Finding for PMID 3.",
    ]
    assert calls == 1


@pytest.mark.asyncio
async def test_synthesize_gene_annotation_reuses_persistent_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(synthesis.settings, "llm_cache_enabled", True)

    extraction_calls = 0
    synthesis_calls = 0

    async def fake_extract_evidence_packets(*args, **kwargs):
        nonlocal extraction_calls
        extraction_calls += 1
        record = args[1][0]
        return [
            {
                "record": record,
                "evidence": {
                    "relevance": "direct",
                    "evidence_types": ["functional assay"],
                    "key_findings": ["TP53 loss promotes cancer growth."],
                    "cancer_types": ["solid tumor"],
                    "caveats": [],
                },
            }
        ]

    async def fake_complete_with_tool(**kwargs):
        nonlocal synthesis_calls
        synthesis_calls += 1
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Functional assay evidence.",
            "cancer_associated_gene_tier": "Class II - Likely Driver",
            "og_or_tsg": "TSG",
            "gene_summary": "TP53 has direct cancer evidence (PMID 12345).",
            "citations": ["12345"],
            "confidence": 0.8,
        }

    monkeypatch.setattr(synthesis, "_extract_evidence_packets", fake_extract_evidence_packets)
    monkeypatch.setattr(synthesis, "complete_with_tool", fake_complete_with_tool)

    record = LiteratureRecord(
        pmid="12345",
        title="TP53 functional evidence",
        abstract="TP53 loss promotes cancer growth in a functional assay.",
    )

    first = await synthesis.synthesize_gene_annotation(
        "TP53",
        ["TP53::ALK"],
        True,
        "solid tumor",
        [record],
        retrieval_tier=1,
        gene_identity="HGNC name: tumor protein p53",
    )
    second = await synthesis.synthesize_gene_annotation(
        "TP53",
        ["TP53::ALK"],
        True,
        "solid tumor",
        [record],
        retrieval_tier=1,
        gene_identity="HGNC name: tumor protein p53",
    )

    assert first == second
    assert extraction_calls == 1
    assert synthesis_calls == 1
