"""Tests for citation precision controls."""

from src.models.schema import LiteratureRecord
from src.pipeline.citation_precision import filter_and_rank_citations
from src.pipeline.selection import (
    prefilter_records_for_selection,
    select_papers_for_synthesis,
)
from src.pipeline.synthesis import (
    _verify_citations,
    build_gene_annotation,
    synthesize_gene_annotation,
)


async def test_selection_preserves_model_relevance_order(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        return {"selected_pmids": ["333", "111", "333", "999", "222"]}

    monkeypatch.setattr("src.pipeline.selection.complete_with_tool", fake_complete_with_tool)

    records = [
        LiteratureRecord(pmid="111", title="First", abstract="Abstract 1"),
        LiteratureRecord(pmid="222", title="Second", abstract="Abstract 2"),
        LiteratureRecord(pmid="333", title="Third", abstract="Abstract 3"),
    ]

    selected = await select_papers_for_synthesis("GENE", records, max_papers=2)

    assert [record.pmid for record in selected] == ["333", "111"]


async def test_selection_can_abstain_when_no_papers_are_relevant(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        return {"selected_pmids": []}

    monkeypatch.setattr("src.pipeline.selection.complete_with_tool", fake_complete_with_tool)

    records = [
        LiteratureRecord(pmid=str(i), title=f"Paper {i}", abstract=f"Abstract {i}")
        for i in range(10)
    ]

    selected = await select_papers_for_synthesis("GENE", records, max_papers=2)

    assert selected == []


async def test_selection_prefilters_and_chunks_before_model_selection(monkeypatch):
    calls = []

    async def fake_complete_with_tool(**kwargs):
        calls.append(kwargs["user"])
        if "final merge" in kwargs["user"]:
            return {"selected_pmids": ["strong-1", "strong-2"]}
        pmids = [
            line.removeprefix("PMID ").strip()
            for line in kwargs["user"].splitlines()
            if line.startswith("PMID ")
        ]
        return {"selected_pmids": pmids[:2]}

    monkeypatch.setattr("src.pipeline.selection.complete_with_tool", fake_complete_with_tool)
    monkeypatch.setattr("src.pipeline.selection.settings.selection_prefilter_limit", 6)
    monkeypatch.setattr("src.pipeline.selection.settings.selection_chunk_size", 3)
    monkeypatch.setattr("src.pipeline.selection.settings.selection_chunk_keep", 2)

    records = [
        LiteratureRecord(
            pmid="strong-1",
            title="GENE knockdown suppresses carcinoma",
            abstract="GENE knockdown reduced tumor proliferation and invasion.",
        ),
        LiteratureRecord(
            pmid="weak-1",
            title="GENE appears in a broad expression signature",
            abstract="A panel of differentially expressed genes was reported.",
        ),
        LiteratureRecord(
            pmid="strong-2",
            title="GENE mutation in cancer cohort",
            abstract="GENE mutation was recurrent in carcinoma and linked to survival.",
        ),
        *[
            LiteratureRecord(
                pmid=f"irrelevant-{i}",
                title=f"Unrelated biology {i}",
                abstract="Developmental biology without tumor context.",
            )
            for i in range(10)
        ],
    ]

    selected = await select_papers_for_synthesis("GENE", records, max_papers=2)

    assert [record.pmid for record in selected] == ["strong-1", "strong-2"]
    assert len(calls) == 3
    assert all("Evidence-bearing abstract context" in call for call in calls)
    assert "irrelevant-9" not in "\n".join(calls)


async def test_conservative_token_mode_uses_deterministic_selection(monkeypatch):
    async def fail_complete_with_tool(**kwargs):
        raise AssertionError("selection LLM should not run in conservative token mode")

    monkeypatch.setattr("src.pipeline.selection.complete_with_tool", fail_complete_with_tool)
    monkeypatch.setattr("src.pipeline.selection.settings.token_budget_mode", "conservative")
    monkeypatch.setattr("src.pipeline.selection.settings.selection_prefilter_limit", 6)

    records = [
        LiteratureRecord(
            pmid="strong",
            title="GENE knockdown suppresses carcinoma",
            abstract="GENE knockdown reduced tumor proliferation and xenograft growth.",
        ),
        LiteratureRecord(
            pmid="medium",
            title="GENE mutation in cancer",
            abstract="GENE mutation was observed in carcinoma.",
        ),
        LiteratureRecord(
            pmid="weak",
            title="GENE expression signature",
            abstract="GENE was one of many genes in an expression signature.",
        ),
    ]

    selected = await select_papers_for_synthesis("GENE", records, max_papers=2)

    assert [record.pmid for record in selected] == ["strong", "medium"]


def test_selection_prefilter_prioritizes_direct_cancer_evidence():
    records = [
        LiteratureRecord(
            pmid="weak",
            title="GENE in broad molecular profiling",
            abstract="GENE was one of many differentially expressed genes.",
        ),
        LiteratureRecord(
            pmid="strong",
            title="GENE knockdown suppresses carcinoma",
            abstract="GENE knockdown reduced tumor proliferation and xenograft growth.",
        ),
    ]

    prefiltered = prefilter_records_for_selection("GENE", records, limit=1)

    assert [record.pmid for record in prefiltered] == ["strong"]


def test_verify_citations_deduplicates_rejects_unretrieved_and_caps():
    records = [
        LiteratureRecord(pmid="111", title="GENE cancer", abstract="GENE knockdown in cancer."),
        LiteratureRecord(pmid="222", title="GENE carcinoma", abstract="GENE proliferation."),
        LiteratureRecord(pmid="333", title="GENE tumor", abstract="GENE invasion."),
        LiteratureRecord(pmid="444", title="GENE oncology", abstract="GENE survival."),
    ]
    verified = _verify_citations(
        "GENE",
        ["111", "222", "222", "999", "333"],
        records,
        max_citations=2,
    )

    assert verified == ["111", "222"]


def test_filter_and_rank_citations_rejects_same_symbol_lncRNA_ambiguity():
    records = [
        LiteratureRecord(
            pmid="bad",
            title="The lncRNA RP1 promotes breast cancer progression",
            abstract="lncRNA RP1 increases tumor proliferation and invasion.",
        ),
        LiteratureRecord(
            pmid="good",
            title="RP1 axonemal microtubule associated gene in cancer sequencing",
            abstract="RP1 mutation was evaluated in a cancer cohort.",
        ),
    ]

    citations = filter_and_rank_citations(
        gene="RP1",
        emitted_citations=["bad", "good"],
        records=records,
        max_citations=4,
        gene_identity="HGNC name: RP1 axonemal microtubule associated; Locus type: gene with protein product",
    )

    assert citations == ["good"]


def test_filter_and_rank_citations_prefers_direct_cancer_support():
    records = [
        LiteratureRecord(
            pmid="weak",
            title="GENE appears in a broad expression signature",
            abstract="A panel of differentially expressed genes was reported.",
        ),
        LiteratureRecord(
            pmid="strong",
            title="GENE knockdown suppresses carcinoma proliferation",
            abstract="GENE knockdown reduced tumor invasion and xenograft growth.",
        ),
    ]

    citations = filter_and_rank_citations(
        gene="GENE",
        emitted_citations=["weak", "strong"],
        records=records,
        max_citations=2,
    )

    assert citations == ["strong"]


def test_build_gene_annotation_downgrades_non_oncokb_class_i_call():
    annotation = build_gene_annotation(
        gene="RFX7",
        fusions=["RFX7::LMTK2"],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=[],
        synthesis_result={
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_associated_gene_tier": "Class I - Driver",
            "og_or_tsg": "TSG",
            "confidence": 0.9,
        },
    )

    assert annotation.cancer_associated_gene_tier == "Class II - Likely Driver"
    assert annotation.og_or_tsg == "TSG"


def test_build_gene_annotation_clears_classification_when_insufficient():
    annotation = build_gene_annotation(
        gene="RP1",
        fusions=["RP1::SPIDR"],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=[],
        synthesis_result={
            "cancer_associated": False,
            "insufficient_evidence": True,
            "cancer_associated_gene_tier": "Class II - Likely Driver",
            "og_or_tsg": "OG",
            "confidence": 0.0,
        },
    )

    assert annotation.cancer_associated_gene_tier is None
    assert annotation.og_or_tsg is None


def test_build_gene_annotation_records_all_retrieved_pmids():
    records = [
        LiteratureRecord(pmid="111", title="A", abstract="BRAF cancer mechanism"),
        LiteratureRecord(pmid="222", title="B", abstract="BRAF melanoma cohort"),
        LiteratureRecord(pmid="111", title="A duplicate", abstract="BRAF cancer mechanism"),
    ]

    annotation = build_gene_annotation(
        gene="BRAF",
        fusions=["TP53::BRAF"],
        in_oncokb=True,
        cancer_type_prevalence=None,
        records=records,
        synthesis_result={
            "cancer_associated": True,
            "insufficient_evidence": False,
            "citations": ["111"],
            "confidence": 0.9,
        },
    )

    assert annotation.retrieval_count == 3
    assert annotation.retrieved_pmids == ["111", "222"]


async def test_synthesis_uses_evidence_packets_instead_of_full_abstract(monkeypatch):
    prompts = []

    async def fake_complete_with_tool(**kwargs):
        prompts.append(kwargs)
        if kwargs["tool"]["name"] == "extract_paper_evidence":
            assert "VERY_LONG_CONTEXT" in kwargs["user"]
            return {
                "relevance": "direct",
                "evidence_types": ["functional assay"],
                "key_findings": ["GENE knockdown reduced tumor proliferation."],
                "cancer_types": ["carcinoma"],
                "caveats": [],
            }
        assert kwargs["tool"]["name"] == "annotate_gene"
        assert "GENE knockdown reduced tumor proliferation." in kwargs["user"]
        assert "VERY_LONG_CONTEXT" not in kwargs["user"]
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "citations": ["111"],
            "confidence": 0.8,
        }

    monkeypatch.setattr("src.pipeline.synthesis.complete_with_tool", fake_complete_with_tool)
    monkeypatch.setattr("src.pipeline.synthesis.settings.evidence_extraction_batch_size", 1)
    monkeypatch.setattr("src.pipeline.synthesis.settings.llm_cache_enabled", False)

    records = [
        LiteratureRecord(
            pmid="111",
            title="GENE carcinoma functional assay",
            abstract="VERY_LONG_CONTEXT " * 200,
        )
    ]

    result = await synthesize_gene_annotation(
        gene="GENE",
        fusions=["GENE::PARTNER"],
        in_oncokb=True,
        cancer_type_prevalence=None,
        records=records,
    )

    assert result["citations"] == ["111"]
    assert [call["tool"]["name"] for call in prompts] == [
        "extract_paper_evidence",
        "annotate_gene",
    ]
