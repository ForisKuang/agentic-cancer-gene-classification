"""Tests for the OpenEvidence sidecar: deterministic distillation
(src.pipeline.openevidence.distill_openevidence), the standalone
GET /v1/genes/{gene}/openevidence endpoint, and proof that core gene
annotation (_annotate_gene) never waits on OpenEvidence.

OpenEvidence's 130-185s call latency and raw-prose synthesis-prompt
dumping were removed from the synchronous annotation path entirely (see
orchestrator.py and synthesis.py) — it now only runs behind the sidecar
endpoint below, distilled deterministically (no LLM call) before being
returned.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from src import main
from src.models.schema import (
    LiteratureRecord,
    OpenEvidenceAnalysis,
    OpenEvidenceCitation,
    ResolvedGene,
)
from src.pipeline import orchestrator
from src.pipeline.openevidence import (
    _split_sentences_with_citation_keys,
    distill_additive_openevidence,
    distill_openevidence,
    distilled_openevidence_has_additive_content,
    is_non_pubmed_sourced_citation,
)

@pytest.fixture(autouse=True)
def _isolated_sidecar_redis(fake_redis):
    """The sidecar endpoint peeks the OpenEvidence cache and claims Redis
    in-flight/failed markers. These tests drive it through TestClient, whose
    request runs on its own event loop — keep it off the shared real Redis
    client (bound to the test's loop, and flushed by other runs) by using
    conftest's in-memory FakeRedis. conftest's _reset_openevidence_sidecar
    clears the sidecar's module state between tests."""
    return fake_redis


# Real, live-captured citation shapes (see tests/test_openevidence.py) reused
# here to keep the distillation fixtures realistic.
_NCCN_CITATION = OpenEvidenceCitation(
    citation_key="1",
    title="Melanoma: Cutaneous",
    journal="",
    date="2026-09-02",
    url="https://www.nccn.org/professionals/physician_gls/pdf/cutaneous_melanoma.pdf#page=77",
)
_ASCO_CITATION = OpenEvidenceCitation(
    citation_key="2",
    title="ASCO Guideline on NSCLC",
    journal="",
    date="2024-01-01",
    url="https://www.asco.org/guidelines/nsclc",
)
_ESMO_CITATION = OpenEvidenceCitation(
    citation_key="3",
    title="ESMO Clinical Practice Guideline",
    journal="",
    date="2023-05-01",
    url="https://www.esmo.org/guidelines/nsclc",
)
_JOURNAL_CITATION = OpenEvidenceCitation(
    citation_key="4",
    title="A pharmacokinetic study of alectinib",
    journal="Annals of Oncology",
    date="2019-03-01",
    doi="10.1000/example",
    url="https://pubmed.ncbi.nlm.nih.gov/30902613",
)

# Three real, live-captured citations from
# benchmarks/results/openevidence_live_20260904/enabled.json that ground the
# guideline-misclassification regression tests below: a genuine ASCO Living
# Guideline (citation_key "16"), a genuine non-guideline JCO clinical trial
# report from the SAME journal (citation_key "35"), and a genuine
# non-guideline cohort-validation research PAPER that merely discusses
# classification guidelines in its title (citation_key "26"). None of the
# three is classifiable correctly by URL domain alone: "16" links via a
# plain pubmed.ncbi.nlm.nih.gov URL indistinguishable by domain from "35",
# and a bare "guideline" substring in the title would wrongly sweep up "26"
# too (it isn't itself a clinical practice guideline).
_ASCO_LIVING_GUIDELINE_VIA_PUBMED_URL = OpenEvidenceCitation(
    citation_key="16",
    title="Therapy for Stage IV Non-Small-Cell Lung Cancer With Driver Alterations: ASCO Living Guideline",
    authors="Singh N, Temin S, Baker S, et al.",
    journal="Journal of Clinical Oncology : Official Journal of the American Society of Clinical Oncology",
    date="2022-10-01",
    doi="10.1200/JCO.22.00824",
    url="https://pubmed.ncbi.nlm.nih.gov/35816666",
)
_JCO_TRIAL_REPORT_CITATION = OpenEvidenceCitation(
    citation_key="35",
    title=(
        "Updated Overall Survival Analysis From the Phase II PHAROS Study of "
        "Encorafenib Plus Binimetinib in Patients With BRAF V600e-Mutant "
        "Metastatic Non-Small Cell Lung Cancer"
    ),
    authors="Johnson ML, Smit EF, Felip E, et al.",
    journal="Journal of Clinical Oncology : Official Journal of the American Society of Clinical Oncology",
    date="2025-12-10",
    doi="10.1200/JCO-25-02023",
    url="https://pubmed.ncbi.nlm.nih.gov/41109959",
)
_COHORT_VALIDATION_CITATION = OpenEvidenceCitation(
    citation_key="26",
    title=(
        "Validation of the 5th edition of the World Health Organization and "
        "International Consensus Classification guidelines for TP53-mutated "
        "myeloid neoplasm in an independent international cohort"
    ),
    authors="Shah MV, Hung K, Baranwal A, et al.",
    journal="Blood Cancer Journal",
    date="2025-05-07",
    doi="10.1038/s41408-025-01290-0",
    url="https://doi.org/10.1038/s41408-025-01290-0",
)

_ALK_ANALYSIS = OpenEvidenceAnalysis(
    question="Is ALK an oncogene in NSCLC?",
    text=(
        "ALK is classified as an oncogenic driver in ALK-rearranged NSCLC. "
        "[[1]] In the ALEX trial, alectinib demonstrated median PFS of 34.8 "
        "months versus 10.9 months for crizotinib. [[4]] NCCN guidelines "
        "recommend alectinib as first-line therapy. [[2]][[3]]"
    ),
    citations=[_NCCN_CITATION, _ASCO_CITATION, _ESMO_CITATION, _JOURNAL_CITATION],
)


# ---------------------------------------------------------------------------
# distill_openevidence
# ---------------------------------------------------------------------------


def test_distill_openevidence_extracts_guideline_from_nccn_url():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    nccn = next(g for g in distilled.guidelines if "nccn.org" in g.url)
    assert nccn.title == "Melanoma: Cutaneous"
    assert nccn.page_anchor == "page=77"


def test_distill_openevidence_extracts_asco_and_esmo_guidelines():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    urls = {g.url for g in distilled.guidelines}
    assert _ASCO_CITATION.url in urls
    assert _ESMO_CITATION.url in urls


def test_distill_openevidence_ignores_non_guideline_journal_citation():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    urls = {g.url for g in distilled.guidelines}
    assert _JOURNAL_CITATION.url not in urls
    assert len(distilled.guidelines) == 3


def test_distill_openevidence_extracts_trial_mention_by_known_acronym():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    alex_mentions = [m for m in distilled.trial_mentions if m.trial == "ALEX"]
    assert len(alex_mentions) == 1
    assert "34.8" in alex_mentions[0].sentence
    assert "PFS" in alex_mentions[0].sentence


def test_distill_openevidence_extracts_trial_mention_by_outcome_statistic_without_acronym():
    analysis = OpenEvidenceAnalysis(
        question="q",
        text="Median OS was not reached in the treatment arm. HR was 0.43.",
    )
    distilled = distill_openevidence(analysis)

    assert len(distilled.trial_mentions) == 2
    assert all(mention.trial is None for mention in distilled.trial_mentions)


def test_distill_openevidence_extracts_consensus_role_as_opening_sentence():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    assert distilled.consensus_role == (
        "ALK is classified as an oncogenic driver in ALK-rearranged NSCLC."
    )
    # Inline citation markers are stripped, not leaked into the UI text.
    assert "[[1]]" not in distilled.consensus_role


def test_distill_openevidence_citation_count_matches_citations():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    assert distilled.citation_count == 4


def test_distill_openevidence_handles_empty_analysis():
    distilled = distill_openevidence(OpenEvidenceAnalysis(question="q", text=""))

    assert distilled.consensus_role is None
    assert distilled.guidelines == []
    assert distilled.trial_mentions == []
    assert distilled.citation_count == 0


# ---------------------------------------------------------------------------
# distill_additive_openevidence / distilled_openevidence_has_additive_content
#
# The additivity filter: a guideline/trial-registry citation is always kept
# (our own PubMed-abstract-only retrieval structurally cannot produce that
# content), but a PubMed-sourced (journal article) citation is dropped when
# it strongly matches (by PMID extracted from a pubmed.ncbi.nlm.nih.gov URL,
# or by normalized title) something the core pipeline's own GeneAnnotation
# already surfaced for this gene.
# ---------------------------------------------------------------------------


def test_distill_additive_openevidence_filters_out_pubmed_citation_overlapping_core_evidence():
    """A PubMed-sourced citation whose PMID matches the core pipeline's own
    verified citations is pure redundancy — dropped entirely, and counted."""
    analysis = OpenEvidenceAnalysis(
        question="q",
        text="A pharmacokinetic study of alectinib was published. [[4]]",
        citations=[_JOURNAL_CITATION],
    )

    distilled = distill_additive_openevidence(analysis, core_pmids=["30902613"])

    assert distilled.citation_count == 0
    assert distilled.redundant_citation_count == 1
    assert distilled.guidelines == []
    assert not distilled_openevidence_has_additive_content(distilled)


def test_distill_additive_openevidence_keeps_non_pubmed_guideline_citation_regardless_of_overlap():
    """A guideline citation is always additive, even if its title happens to
    match a core-pipeline title — guideline content is structurally
    unreachable by PubMed-abstract retrieval, so overlap is impossible in
    practice and irrelevant to the rule."""
    analysis = OpenEvidenceAnalysis(
        question="q",
        text="NCCN guidelines recommend targeted therapy. [[1]]",
        citations=[_NCCN_CITATION],
    )

    distilled = distill_additive_openevidence(
        analysis, core_pmids=[], core_titles=[_NCCN_CITATION.title]
    )

    assert distilled.citation_count == 1
    assert distilled.redundant_citation_count == 0
    assert len(distilled.guidelines) == 1
    assert distilled_openevidence_has_additive_content(distilled)


def test_distill_additive_openevidence_mixed_case_keeps_guideline_drops_overlapping_journal_citation():
    analysis = OpenEvidenceAnalysis(
        question="q",
        text=(
            "NCCN guidelines recommend alectinib as first-line therapy. [[1]] "
            "A pharmacokinetic study of alectinib was published. [[4]]"
        ),
        citations=[_NCCN_CITATION, _JOURNAL_CITATION],
    )

    distilled = distill_additive_openevidence(analysis, core_pmids=["30902613"])

    assert distilled.citation_count == 1
    assert distilled.redundant_citation_count == 1
    assert [g.url for g in distilled.guidelines] == [_NCCN_CITATION.url]
    assert distilled_openevidence_has_additive_content(distilled)


def test_distill_additive_openevidence_matches_overlap_by_normalized_title_when_no_pmid_url():
    """A citation without a pubmed.ncbi.nlm.nih.gov-shaped URL (e.g. a doi.org
    link) can still be recognized as redundant via a normalized title match
    against the core pipeline's evidence_cards titles."""
    citation = OpenEvidenceCitation(
        citation_key="9",
        title="Acetyl-CoA metabolism in cancer",
        journal="Nature Reviews. Cancer",
        date="2023-03-01",
        doi="10.1038/s41568-022-00543-5",
        url="https://doi.org/10.1038/s41568-022-00543-5",
    )
    analysis = OpenEvidenceAnalysis(question="q", text="See [[9]].", citations=[citation])

    distilled = distill_additive_openevidence(
        analysis, core_titles=["Acetyl-CoA Metabolism In Cancer!"]
    )

    assert distilled.citation_count == 0
    assert distilled.redundant_citation_count == 1


def test_distill_additive_openevidence_with_no_core_evidence_keeps_all_citations():
    """No core_pmids/core_titles supplied (an older client, or no annotation
    in hand yet) means nothing is filtered out as redundant — identical to
    calling distill_openevidence directly."""
    distilled = distill_additive_openevidence(_ALK_ANALYSIS)

    assert distilled.citation_count == 4
    assert distilled.redundant_citation_count == 0
    assert len(distilled.guidelines) == 3


# ---------------------------------------------------------------------------
# Guideline misclassification regression, round 2 (real fixtures from
# benchmarks/results/openevidence_live_20260904/enabled.json): a bare
# "guideline" title substring was too broad (it wrongly swept up
# _COHORT_VALIDATION_CITATION, a real research paper that merely discusses
# classification guidelines) and unconditionally trusting the ascopubs.org
# domain was too broad the other direction (it wrongly exempted an ordinary
# JCO paper whenever its URL happened to be publisher-hosted rather than a
# pubmed.ncbi.nlm.nih.gov link). The fix requires a specific
# guideline-issuing society name (ASCO/NCCN/ESMO) anchored directly against
# "Guideline(s)" at the title's end, or the authors field being exactly a
# guideline-issuing organization's name — see _GUIDELINE_TITLE_PATTERN's
# docstring.
# ---------------------------------------------------------------------------


def test_asco_living_guideline_via_pubmed_url_is_classified_non_pubmed_sourced_by_title():
    """citation_key '16': linked via a plain pubmed.ncbi.nlm.nih.gov URL,
    domain-indistinguishable from _JCO_TRIAL_REPORT_CITATION below — only
    the anchored 'ASCO ... Guideline' title pattern identifies it."""
    assert is_non_pubmed_sourced_citation(_ASCO_LIVING_GUIDELINE_VIA_PUBMED_URL) is True


def test_real_jco_trial_report_is_droppable_on_overlap_not_always_additive():
    """citation_key '35': a genuine non-guideline JCO clinical trial report,
    same journal and URL shape as the guideline above. Must NOT be treated
    as always-additive — it's an ordinary PubMed-sourced paper, subject to
    the normal overlap check like any other."""
    assert is_non_pubmed_sourced_citation(_JCO_TRIAL_REPORT_CITATION) is False


def test_real_jco_trial_report_does_not_flip_when_hosted_via_ascopubs_url():
    """The same PHAROS trial report, but linked via ascopubs.org (ASCO's
    general journal-hosting platform) instead of pubmed.ncbi.nlm.nih.gov.
    Trusting ascopubs.org as a blanket guideline-domain signal would flip
    this to non-PubMed-sourced even though it is still an ordinary trial
    report — reproduces and guards against that regression."""
    citation_via_ascopubs = _JCO_TRIAL_REPORT_CITATION.model_copy(
        update={"url": "https://ascopubs.org/doi/10.1200/JCO-25-02023"}
    )
    assert is_non_pubmed_sourced_citation(citation_via_ascopubs) is False


def test_cohort_validation_paper_discussing_guidelines_is_not_classified_as_guideline():
    """citation_key '26': a real cohort-validation research paper whose
    title contains the word 'guidelines' (discussing WHO/ICC classification
    guidelines) without being an NCCN/ASCO/ESMO clinical practice guideline
    itself. A bare 'guideline' substring match would wrongly keep this as
    always-additive; the anchored society-name pattern correctly excludes
    it, leaving it subject to the normal overlap check."""
    assert is_non_pubmed_sourced_citation(_COHORT_VALIDATION_CITATION) is False


def test_distill_additive_openevidence_keeps_asco_living_guideline_regardless_of_pmid_overlap():
    """The real ASCO Living Guideline citation stays additive even when its
    own PMID is passed as core_pmids — a guideline citation is always
    additive regardless of overlap (see is_additive_citation)."""
    analysis = OpenEvidenceAnalysis(
        question="q",
        text="See the guideline citation. [[16]]",
        citations=[_ASCO_LIVING_GUIDELINE_VIA_PUBMED_URL],
    )

    distilled = distill_additive_openevidence(analysis, core_pmids=["35816666"])

    assert distilled.citation_count == 1
    assert distilled.redundant_citation_count == 0


def test_distill_additive_openevidence_drops_real_jco_trial_report_and_cohort_validation_paper_on_overlap():
    """Both real non-guideline citations (the PHAROS trial report and the
    cohort-validation paper) are dropped as redundant once their own
    PMID/title is passed as core evidence — confirming neither is
    incorrectly treated as always-additive."""
    analysis = OpenEvidenceAnalysis(
        question="q",
        text="See these papers. [[35]][[26]]",
        citations=[_JCO_TRIAL_REPORT_CITATION, _COHORT_VALIDATION_CITATION],
    )

    distilled = distill_additive_openevidence(
        analysis,
        core_pmids=["41109959"],
        core_titles=[_COHORT_VALIDATION_CITATION.title],
    )

    assert distilled.citation_count == 0
    assert distilled.redundant_citation_count == 2


# ---------------------------------------------------------------------------
# Trial-mention redundancy leak regression: a trial/outcome sentence whose
# ONLY backing citation was dropped as redundant must not leak the same
# information back in unfiltered. See _filter_additive_trial_mentions.
# ---------------------------------------------------------------------------


def test_distill_additive_openevidence_drops_trial_mention_solely_backed_by_redundant_citation():
    """The ALEX trial/PFS sentence is backed only by citation [[4]]
    (_JOURNAL_CITATION). When that citation is dropped as redundant (its
    PMID matches a core_pmids entry), the mention describing the same paper
    must be dropped too — otherwise the same information leaks back in via
    trial_mentions even though the citation was correctly removed."""
    distilled = distill_additive_openevidence(_ALK_ANALYSIS, core_pmids=["30902613"])

    assert distilled.redundant_citation_count == 1
    assert distilled.trial_mentions == []
    # The 3 guideline citations (always additive) still keep the card
    # available — this test isolates the trial-mention leak fix, not the
    # "nothing additive at all" case (covered by the endpoint-level test
    # test_openevidence_sidecar_endpoint_returns_unavailable_when_only_content_is_a_trial_mention_of_a_dropped_citation).
    assert len(distilled.guidelines) == 3
    assert distilled_openevidence_has_additive_content(distilled)


def test_distill_additive_openevidence_keeps_trial_mention_with_no_citation_marker():
    """A trial mention with NO inline citation marker at all can't be
    attributed to any (redundant or additive) citation, so it is always
    kept — even in the same analysis as a mention that IS dropped for being
    solely backed by a redundant citation. This demonstrates the filter is
    selective, not a blanket drop of every trial mention when any citation
    is redundant."""
    analysis = OpenEvidenceAnalysis(
        question="q",
        text=(
            "In the ALEX trial, alectinib demonstrated median PFS of 34.8 "
            "months versus 10.9 months for crizotinib. [[4]] The CROWN trial "
            "showed similar benefit with lorlatinib."
        ),
        citations=[_JOURNAL_CITATION],
    )

    distilled = distill_additive_openevidence(analysis, core_pmids=["30902613"])

    assert distilled.redundant_citation_count == 1
    assert [m.trial for m in distilled.trial_mentions] == ["CROWN"]
    assert distilled_openevidence_has_additive_content(distilled)


def test_distill_additive_openevidence_keeps_trial_mention_backed_by_guideline_citation():
    """A trial mention backed by a guideline/trial-registry citation is kept
    regardless of any overlap check, since that citation is always additive
    — the mention is not 'redundant', it's guideline-sourced content our
    own retrieval structurally can't produce either way."""
    analysis = OpenEvidenceAnalysis(
        question="q",
        text="In the FLAURA trial, osimertinib improved PFS. [[1]]",
        citations=[_NCCN_CITATION],
    )

    distilled = distill_additive_openevidence(analysis)

    assert [m.trial for m in distilled.trial_mentions] == ["FLAURA"]


# ---------------------------------------------------------------------------
# Marker-to-sentence association regression, round 2: the original linkage
# only recognized a citation marker immediately trailing a sentence's own
# terminal punctuation. Real captured prose (benchmarks/results/) places
# markers anywhere in a sentence's span — mid-clause, inside parentheses, or
# with a preceding space right before the period — and the association must
# also handle a final sentence with no terminal punctuation at all.
# ---------------------------------------------------------------------------


def test_split_sentences_with_citation_keys_finds_marker_immediately_before_terminal_punctuation():
    """'ALEX improved PFS [[1]].' — the marker sits before the sentence's own
    period (with a preceding space), not after it. Must still be recognized
    as backing this sentence."""
    pairs = _split_sentences_with_citation_keys("ALEX improved PFS [[1]].")

    assert len(pairs) == 1
    sentence, keys = pairs[0]
    assert keys == ["1"]
    assert "[[1]]" not in sentence


def test_split_sentences_with_citation_keys_finds_internal_mid_sentence_marker():
    """'ALEX [[2]] improved PFS. [[1]]' — key 2 is embedded mid-sentence
    (before the sentence's own terminal punctuation), key 1 trails after it.
    Both must be recognized as backing this one sentence."""
    pairs = _split_sentences_with_citation_keys("ALEX [[2]] improved PFS. [[1]]")

    assert len(pairs) == 1
    sentence, keys = pairs[0]
    assert set(keys) == {"1", "2"}
    assert "[[" not in sentence


def test_split_sentences_with_citation_keys_finds_marker_inside_parentheses_real_flaura_shape():
    """Real captured FLAURA/FLAURA2 prose (benchmarks/results/
    openevidence_pointed_20260908/enabled.json): an internal marker sits
    inside a parenthetical HR statistic, nowhere near the sentence's own
    terminal punctuation, which comes much later in the same sentence."""
    text = (
        "FLAURA established osimertinib over first-generation TKIs (median PFS "
        "18.9 vs 10.2 months; median OS 38.6 vs 31.8 months), and FLAURA2 "
        "reported median OS 47.5 vs 37.6 months with added platinum–pemetrexed "
        "(HR 0.77[[20]]) at the cost of grade ≥3 adverse events in 70% vs 34%."
    )

    pairs = _split_sentences_with_citation_keys(text)

    assert len(pairs) == 1
    sentence, keys = pairs[0]
    assert keys == ["20"]
    assert "[[20]]" not in sentence
    assert "FLAURA2" in sentence


def test_split_sentences_with_citation_keys_includes_trailing_text_with_no_terminal_punctuation():
    """'ALEX improved PFS' (no '.', '!', or '?') must still produce a
    sentence segment — treating end-of-string as an implicit boundary —
    rather than vanishing entirely."""
    pairs = _split_sentences_with_citation_keys("ALEX improved PFS")

    assert pairs == [("ALEX improved PFS", [])]


def test_distill_additive_openevidence_keeps_uncited_trial_mention_with_no_terminal_punctuation():
    """End-to-end: a trial mention with no terminal punctuation and no
    citation marker must survive distill_additive_openevidence and keep the
    sidecar available, even when unrelated citations elsewhere in the same
    analysis are dropped as redundant. Before the end-of-string fix, this
    mention would silently vanish (zero sentence pairs found at all),
    which could wrongly flip the whole response to unavailable."""
    analysis = OpenEvidenceAnalysis(
        question="q",
        text="A pharmacokinetic study of alectinib was published. [[4]] ALEX improved PFS",
        citations=[_JOURNAL_CITATION],
    )

    distilled = distill_additive_openevidence(analysis, core_pmids=["30902613"])

    assert distilled.redundant_citation_count == 1
    assert [m.trial for m in distilled.trial_mentions] == ["ALEX"]
    assert distilled_openevidence_has_additive_content(distilled)


# ---------------------------------------------------------------------------
# GET /v1/genes/{gene}/openevidence
# ---------------------------------------------------------------------------


def test_openevidence_sidecar_endpoint_returns_unavailable_when_disabled(monkeypatch):
    monkeypatch.setattr(main.settings, "openevidence_enabled", False)

    async def fail_if_called(self, *args, **kwargs):
        raise AssertionError("OpenEvidence should never be called when disabled")

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fail_if_called)
    client = TestClient(main.app)

    response = client.get("/v1/genes/ALK/openevidence")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["distilled"] is None


def test_openevidence_sidecar_endpoint_returns_distilled_result_when_enabled(monkeypatch):
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)

    async def fake_get_gene_analysis(self, gene, tumor_type=None, fusion=None, client=None):
        assert gene == "ALK"
        return _ALK_ANALYSIS

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_get_gene_analysis)
    client = TestClient(main.app)

    response = client.get("/v1/genes/ALK/openevidence", params={"tumor_type": "NSCLC"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["distilled"]["citation_count"] == 4
    assert len(payload["distilled"]["guidelines"]) == 3
    assert any(m["trial"] == "ALEX" for m in payload["distilled"]["trial_mentions"])


def test_openevidence_sidecar_endpoint_filters_redundant_citations_via_core_pmids(monkeypatch):
    """The endpoint's own core_pmids/core_titles params (the caller's
    already-computed GeneAnnotation evidence) drop the one PubMed-sourced
    citation that overlaps, while the three guideline citations survive."""
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)

    async def fake_get_gene_analysis(self, gene, tumor_type=None, fusion=None, client=None):
        return _ALK_ANALYSIS

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_get_gene_analysis)
    client = TestClient(main.app)

    response = client.get(
        "/v1/genes/ALK/openevidence",
        params={"core_pmids": ["30902613"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["distilled"]["citation_count"] == 3
    assert payload["distilled"]["redundant_citation_count"] == 1
    assert len(payload["distilled"]["guidelines"]) == 3


def test_openevidence_sidecar_endpoint_returns_unavailable_when_nothing_additive_survives_filter(
    monkeypatch,
):
    """When every citation is PubMed-sourced and overlaps the core pipeline's
    own evidence, and the analysis text has no trial/outcome-statistic
    mention, nothing additive remains — the endpoint reports unavailable
    rather than rendering an empty-looking card."""
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)
    analysis = OpenEvidenceAnalysis(
        question="q",
        text="A pharmacokinetic study of alectinib was published.",
        citations=[_JOURNAL_CITATION],
    )

    async def fake_get_gene_analysis(self, gene, tumor_type=None, fusion=None, client=None):
        return analysis

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_get_gene_analysis)
    client = TestClient(main.app)

    response = client.get(
        "/v1/genes/ALK/openevidence",
        params={"core_pmids": ["30902613"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["distilled"] is None


def test_openevidence_sidecar_endpoint_returns_unavailable_when_only_content_is_a_trial_mention_of_a_dropped_citation(
    monkeypatch,
):
    """Reproduces the trial-mention redundancy leak: one PubMed citation
    that overlaps a core PMID (correctly dropped) plus a trial-mention
    sentence describing that SAME paper (an ALEX trial/PFS statistic sourced
    from citation [[4]], _JOURNAL_CITATION). Before the fix, this reported
    available=true purely because trial_mentions was non-empty, even though
    the only citation backing it had just been dropped as redundant. After
    the fix, the mention is dropped along with its citation and nothing
    additive remains."""
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)
    analysis = OpenEvidenceAnalysis(
        question="q",
        text=(
            "In the ALEX trial, alectinib demonstrated median PFS of 34.8 "
            "months versus 10.9 months for crizotinib. [[4]]"
        ),
        citations=[_JOURNAL_CITATION],
    )

    async def fake_get_gene_analysis(self, gene, tumor_type=None, fusion=None, client=None):
        return analysis

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_get_gene_analysis)
    client = TestClient(main.app)

    response = client.get(
        "/v1/genes/ALK/openevidence",
        params={"core_pmids": ["30902613"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["distilled"] is None


def test_openevidence_sidecar_endpoint_skips_live_call_when_confidently_not_cancer_associated(
    monkeypatch,
):
    """cancer_associated=False and insufficient_evidence=False (our own
    pipeline's confident conclusion) means the guideline/trial-focused
    question has nothing plausible to find — see the live value benchmark's
    RP1/CLCN3P1/DENND2C cases, all of which had cancer_associated=False and
    zero measurable OpenEvidence improvement. The call is skipped entirely,
    never reaching OpenEvidenceClient."""
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)

    async def fail_if_called(self, *args, **kwargs):
        raise AssertionError("OpenEvidence should be skipped, not called")

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fail_if_called)
    client = TestClient(main.app)

    response = client.get(
        "/v1/genes/RP1/openevidence",
        params={"cancer_associated": "false", "insufficient_evidence": "false"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False


def test_openevidence_sidecar_endpoint_still_calls_when_not_cancer_associated_but_evidence_insufficient(
    monkeypatch,
):
    """cancer_associated=False paired with insufficient_evidence=True means
    our own pipeline's "not cancer-associated" conclusion is itself weakly
    supported (sparse/no retrieved literature) — not the confident case the
    gate is meant to catch — so the live call still proceeds."""
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)

    async def fake_get_gene_analysis(self, gene, tumor_type=None, fusion=None, client=None):
        return _ALK_ANALYSIS

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_get_gene_analysis)
    client = TestClient(main.app)

    response = client.get(
        "/v1/genes/AIRE/openevidence",
        params={"cancer_associated": "false", "insufficient_evidence": "true"},
    )

    assert response.status_code == 200
    assert response.json()["available"] is True


def test_openevidence_sidecar_endpoint_calls_when_cancer_associated_true(monkeypatch):
    """cancer_associated=True always proceeds — guideline/trial evidence is
    orthogonal value regardless of how well-supported the classification
    already is (see _build_question's docstring: BRAF/EGFR/KRAS/etc. still
    benefit from guideline citations despite already having strong verified
    citations of their own)."""
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)

    async def fake_get_gene_analysis(self, gene, tumor_type=None, fusion=None, client=None):
        return _ALK_ANALYSIS

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_get_gene_analysis)
    client = TestClient(main.app)

    response = client.get(
        "/v1/genes/BRAF/openevidence",
        params={"cancer_associated": "true"},
    )

    assert response.status_code == 200
    assert response.json()["available"] is True


def test_openevidence_sidecar_endpoint_calls_when_cancer_associated_omitted(monkeypatch):
    """A caller without an annotation in hand yet (or an older client) omits
    cancer_associated entirely — the gate must not trigger on that default,
    so the live call proceeds exactly as before this change."""
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)

    async def fake_get_gene_analysis(self, gene, tumor_type=None, fusion=None, client=None):
        return _ALK_ANALYSIS

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_get_gene_analysis)
    client = TestClient(main.app)

    response = client.get("/v1/genes/ALK/openevidence")

    assert response.status_code == 200
    assert response.json()["available"] is True


def test_openevidence_sidecar_endpoint_returns_unavailable_on_lookup_failure(monkeypatch):
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)

    async def fake_failing_lookup(self, gene, tumor_type=None, fusion=None, client=None):
        raise RuntimeError("upstream timeout")

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_failing_lookup)
    client = TestClient(main.app)

    response = client.get("/v1/genes/ALK/openevidence")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert "upstream timeout" in payload["error"]


# ---------------------------------------------------------------------------
# _annotate_gene never waits on OpenEvidence
# ---------------------------------------------------------------------------


async def test_annotate_gene_never_touches_openevidence_even_when_enabled(monkeypatch):
    """OPENEVIDENCE_ENABLED=true must have zero effect on _annotate_gene:
    no "openevidence" timing bucket, and orchestrator.py no longer imports
    anything from src.pipeline.openevidence to call in the first place."""
    monkeypatch.setattr(orchestrator.settings, "openevidence_enabled", True)
    assert not hasattr(orchestrator, "OpenEvidenceClient")
    assert not hasattr(orchestrator, "_maybe_fetch_openevidence_context")

    async def fake_check_oncokb_membership(gene, lookup=None):
        return False

    async def fake_retrieve_literature(*args, **kwargs):
        return (
            [
                LiteratureRecord(
                    pmid="123",
                    title="ALK cancer",
                    abstract="ALK was studied in cancer.",
                    publication_types=["Journal Article"],
                )
            ],
            1,
        )

    async def fake_select_papers(*args, **kwargs):
        return args[1]

    async def fake_synthesize_gene_annotation(*args, **kwargs):
        assert "openevidence_context" not in kwargs
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Retrieved literature supports a cancer association.",
            "gene_summary": "ALK has retrieved cancer evidence (PMID 123).",
            "citations": ["123"],
        }

    monkeypatch.setattr(orchestrator, "check_oncokb_membership", fake_check_oncokb_membership)
    monkeypatch.setattr(orchestrator, "retrieve_literature", fake_retrieve_literature)
    monkeypatch.setattr(orchestrator, "select_papers_for_synthesis", fake_select_papers)
    monkeypatch.setattr(
        orchestrator, "synthesize_gene_annotation", fake_synthesize_gene_annotation
    )

    annotation = await orchestrator._annotate_gene(
        gene="ALK",
        fusions=[],
        resolved_gene=ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=True),
        unresolvable=False,
    )

    assert "openevidence" not in annotation.timings_ms
    assert annotation.timings_ms["total"] < 15000


# ---------------------------------------------------------------------------
# Sidecar endpoint concurrency limiter: a batch result page can render one
# card per gene, each firing its own request the instant it mounts. Without
# a limiter, that's N concurrent live OpenEvidence calls with nothing left
# to throttle them once OpenEvidence was taken off annotation_gene_concurrency's
# gated path (see orchestrator.py's _annotate_gene). settings.openevidence_
# sidecar_concurrency caps concurrent live calls across ALL requests to this
# endpoint (see main.py's _openevidence_sidecar_semaphore).
# ---------------------------------------------------------------------------


async def test_openevidence_sidecar_endpoint_caps_concurrent_live_calls(monkeypatch):
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)
    monkeypatch.setattr(main.settings, "openevidence_sidecar_concurrency", 1)
    # Force a fresh semaphore for this test's limit value rather than reusing
    # one created (at a different limit) by an earlier test in this process.
    main._openevidence_sidecar_semaphores.clear()

    active = 0
    max_observed_active = 0
    release = asyncio.Event()

    async def fake_get_gene_analysis(self, gene, tumor_type=None, fusion=None, client=None):
        nonlocal active, max_observed_active
        active += 1
        max_observed_active = max(max_observed_active, active)
        await release.wait()
        active -= 1
        return _ALK_ANALYSIS

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_get_gene_analysis)

    calls = asyncio.gather(
        main.get_gene_openevidence("ALK"),
        main.get_gene_openevidence("BRAF"),
        main.get_gene_openevidence("EGFR"),
    )
    await asyncio.sleep(0.05)  # let all three requests reach the semaphore
    assert max_observed_active == 1  # never more than the configured cap of 1
    release.set()
    results = await calls

    assert all(result.available for result in results)


async def test_openevidence_sidecar_endpoint_allows_concurrency_up_to_the_configured_cap(
    monkeypatch,
):
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)
    monkeypatch.setattr(main.settings, "openevidence_sidecar_concurrency", 2)
    main._openevidence_sidecar_semaphores.clear()

    active = 0
    max_observed_active = 0
    release = asyncio.Event()

    async def fake_get_gene_analysis(self, gene, tumor_type=None, fusion=None, client=None):
        nonlocal active, max_observed_active
        active += 1
        max_observed_active = max(max_observed_active, active)
        await release.wait()
        active -= 1
        return _ALK_ANALYSIS

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_get_gene_analysis)

    calls = asyncio.gather(
        main.get_gene_openevidence("ALK"),
        main.get_gene_openevidence("BRAF"),
    )
    await asyncio.sleep(0.05)
    assert max_observed_active == 2  # both allowed through at once, matching the cap
    release.set()
    await calls
