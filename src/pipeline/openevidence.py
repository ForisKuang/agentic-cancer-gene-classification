"""
OpenEvidence supplementary evidence lookup.

Off by default (settings.openevidence_enabled). When enabled, this posts a
closed, targeted question — clinical practice guideline / trial evidence for
targeted therapy of a gene (+ optional tumor type), or of a specific fusion
when one is given (see _build_question) — to OpenEvidence's streaming
analysis endpoint and returns accumulated prose plus a deduplicated citation
list.

Consumed only by the on-demand, non-blocking sidecar endpoint
(GET /v1/genes/{gene}/openevidence in main.py), which distills the analysis
into an independent UI card (see distill_additive_openevidence), and by the
offline cache warmup (src.pipeline.openevidence_warmup). It is NOT part of
core gene annotation or synthesis: OpenEvidence citations are not guaranteed
to be PMIDs in the retrieved literature set, so they must never be passed
through src.pipeline.citation_precision.filter_and_rank_citations or merged
into GeneAnnotation.citations. Callers must always surface this output as
clearly labeled unverified/supplementary evidence.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional, Tuple

import httpx
from tenacity import RetryError, retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.config import settings
from src.models.schema import (
    DistilledOpenEvidence,
    OpenEvidenceAnalysis,
    OpenEvidenceCitation,
    OpenEvidenceGuideline,
    OpenEvidenceTrialMention,
)
from src.pipeline.cache import cached_call
from src.pipeline.normalization import split_fusion

logger = logging.getLogger(__name__)

STREAMING_ANALYSIS_PATH = "/streaming/analysis"

# HTTP statuses worth retrying: request timeout, rate limit, and 5xx. Any other
# 4xx (e.g. 401 bad API key, 404) can never succeed on retry, so fail fast.
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429})


class OpenEvidenceConfigurationError(RuntimeError):
    """Raised when OpenEvidence lookups are requested without required configuration."""


def _is_transient_openevidence_error(exc: BaseException) -> bool:
    """Retry predicate: network/connection errors and 408/429/5xx are
    transient and worth retrying. A permanent 4xx (e.g. 401 bad API key) can
    never succeed on retry, so it fails fast instead of burning the retry
    budget.

    A read/connect/pool timeout is deliberately NOT retried either, even
    though it's "transient" in the usual sense: a live-verified smoke test
    against the real API took ~220s and still hadn't finished a single
    moderately complex clinical question. Retrying a slow-but-functioning
    server would only multiply an already multi-minute wait for what is
    meant to be a quick, best-effort supplementary lookup — a timeout is
    treated as "no supplementary evidence available this time", the same
    normal, non-alarming outcome as any other best-effort lookup failure
    (the sidecar endpoint returns {"available": false} — see
    GET /v1/genes/{gene}/openevidence in main.py).
    """
    if isinstance(exc, httpx.TimeoutException):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in _RETRYABLE_HTTP_STATUSES or status >= 500
    if isinstance(exc, httpx.HTTPError):
        # Any other HTTPError subclass here is a connection/network failure
        # (HTTPStatusError and TimeoutException are already handled above),
        # always transient — e.g. a dropped/reset connection mid-stream,
        # which is how an incomplete stream actually surfaces against the
        # real API (there is no application-level completion sentinel to
        # check for; see _post_streaming_analysis).
        return True
    return False


def _cache_key(gene: str, tumor_type: Optional[str] = None, fusion: Optional[str] = None) -> str:
    """Cache-key derivation for OpenEvidenceClient.get_gene_analysis — the
    single fetch/store path shared by the sidecar endpoint and the offline
    warmup (src.pipeline.openevidence_warmup), so a warmed entry lands in the
    same slot a sidecar request with the same gene/tumor_type/fusion reads.

    `fusion` is included whenever it resolves to two real partner genes (the
    same gating _build_question uses to decide whether to ask the
    fusion-specific question) so that a fusion-specific answer and a
    gene-only answer for the same gene/tumor_type never collide in the same
    cache slot. Normalized via split_fusion (upper-cased, canonical "::"
    separator) so equivalent fusion notations ("EML4::ALK", "EML4--ALK")
    hash to the same key.
    """
    key: Dict[str, str] = {
        "gene": gene.strip().upper(),
        "tumor_type": (tumor_type or "").strip().lower(),
        "model": settings.openevidence_model,
    }
    if fusion:
        gene1, gene2 = split_fusion(fusion)
        if gene1 and gene2:
            key["fusion"] = f"{gene1.strip().upper()}::{gene2.strip().upper()}"
    return "openevidence:" + json.dumps(key, sort_keys=True)


def _build_question(gene: str, tumor_type: Optional[str] = None, fusion: Optional[str] = None) -> str:
    """Build a closed, targeted question aimed at the one content type our
    own PubMed-abstract-only retrieval structurally cannot ever produce:
    clinical practice guideline (NCCN/ASCO/ESMO) and trial-level treatment
    evidence, rather than re-asking the oncogene/tumor-suppressor
    classification our own synthesis prompt already derives from retrieved
    abstracts.

    This replaces an earlier classification question ("is GENE an oncogene
    or tumor suppressor... state the classification") — see
    benchmarks/openevidence_value_report.md and
    benchmarks/openevidence_vendor_assessment.md on the
    agcg-openevidence-vendor-assessment branch. That benchmark found two
    things: (1) on genes our own retrieval already supported well (BRAF,
    EGFR, KRAS, TP53, BRCA1, ALK), OpenEvidence's verified-citation count was
    literally unchanged (4→4) — the classification question was redundant
    with what our own pipeline already does; (2) OpenEvidence's citation set
    included an NCCN guideline reference for 10 of 16 genes regardless of
    how well our own retrieval already supported the gene — content
    genuinely orthogonal to a PubMed-abstract corpus, since guidelines
    aren't indexed in PubMed at all. Asking directly for that content (guidelines/
    trials), instead of a classification restatement, concentrates the
    130-250s call on the evidence type it has demonstrated unique value for.

    `fusion` (a raw "GENE1::GENE2"-style input string, see
    normalization.is_fusion_input) asks about guideline/trial evidence for
    the fusion itself rather than each partner gene in isolation.

    `tumor_type`, when present, replaces the generic "cancer" context rather
    than being appended after it (avoiding an awkward "...in cancer in
    breast cancer?" double-up) — tumor_type names are already
    cancer-specific (e.g. "breast cancer", "melanoma", "NSCLC").
    """
    cancer_context = tumor_type if tumor_type else "cancer"
    if fusion:
        gene1, gene2 = split_fusion(fusion)
        if gene1 and gene2:
            return (
                "What NCCN, ASCO, or ESMO clinical practice guideline "
                "recommendations or clinical trial evidence address targeted "
                f"therapy for the {gene1}::{gene2} fusion in {cancer_context}? "
                "Cite the specific guideline or trial."
            )
    return (
        "What NCCN, ASCO, or ESMO clinical practice guideline recommendations "
        f"or clinical trial evidence address targeted therapy for {gene} "
        f"alterations in {cancer_context}? Cite the specific guideline or trial."
    )


def _iter_sse_payloads(raw: str) -> List[str]:
    """Split an SSE stream body into raw per-event data payloads.

    Each event is a blank-line-separated block containing one or more
    `data:` lines. Per the SSE spec, multiple `data:` lines within one event
    are joined with "\\n" between them (NOT concatenated directly — plain
    concatenation can corrupt JSON payload semantics when a single JSON
    payload is split across lines).
    """
    payloads: List[str] = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line[len("data:"):].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        payloads.append("\n".join(data_lines))
    return payloads


def _parse_sse_events(raw: str) -> List[dict]:
    """Parse an SSE stream body into a list of JSON event payloads.

    There is no application-level stream-termination sentinel in the real
    OpenEvidence API (confirmed absent from both the official docs and a
    live-captured response) — completion is signalled entirely by the HTTP
    response body ending normally, which the transport layer (httpx) is
    responsible for detecting; see _post_streaming_analysis. Malformed
    payloads are skipped rather than failing the whole parse, since a single
    bad delta shouldn't discard everything accumulated so far.
    """
    events: List[dict] = []
    for payload in _iter_sse_payloads(raw):
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed OpenEvidence SSE payload: %r", payload[:200])
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _citation_from_event(event: dict) -> Optional[OpenEvidenceCitation]:
    """Extract citation metadata from a citation-bearing event.

    Confirmed against the real API (official docs + a live-captured
    response): citation data is nested under event["reference"], with
    bibliographic fields a further level deep under
    event["reference"]["reference_detail"] — NOT flat top-level event
    fields. citation_key is an integer on the wire; cast to str for
    OpenEvidenceCitation.citation_key. journal_name is preferred over the
    abbreviated journal_short_name when both are present.
    """
    reference = event.get("reference")
    if not isinstance(reference, dict):
        return None
    citation_key = reference.get("citation_key")
    if citation_key is None:
        return None
    detail = reference.get("reference_detail")
    if not isinstance(detail, dict):
        detail = {}
    source_texts = reference.get("source_texts") or []
    return OpenEvidenceCitation(
        citation_key=str(citation_key),
        reference_text=reference.get("reference_text"),
        publication_info_string=(
            reference.get("publication_info_string") or detail.get("publication_info_string")
        ),
        title=detail.get("title") or "",
        authors=detail.get("authors_string") or "",
        journal=detail.get("journal_name") or detail.get("journal_short_name") or "",
        date=detail.get("publication_date") or "",
        doi=detail.get("doi") or "",
        url=detail.get("url") or "",
        source_texts=[text for text in source_texts if text],
    )


def _strip_generation_step_prefix(text: str) -> str:
    """Remove the frontend widget prefix after joining potentially split deltas.

    Decode JSON to find its exact end, including nested props and braces in
    strings, without consuming any following analysis prose. Leave malformed
    or unrelated text unchanged rather than guessing where the prose begins.
    """
    marker = "REACTCOMPONENT!:!InlineGenerationStep!:!"
    decoder = json.JSONDecoder()
    while text.lstrip().startswith(marker):
        payload = text.lstrip()[len(marker):].lstrip()
        if not payload.startswith("{"):
            break
        try:
            _, end = decoder.raw_decode(payload)
        except json.JSONDecodeError:
            break
        text = payload[end:].lstrip()
    return text


def _build_analysis(question: str, events: List[dict]) -> OpenEvidenceAnalysis:
    """Accumulate prose text and dedupe citations across all events.

    Per the official docs, "concatenating together the text fields from the
    data messages will produce the full analysis text" — this includes
    citation-bearing events, whose `text` is typically an inline marker like
    "[[1]]", not just plain message events. So every event's `text` is
    appended when present, citation or not.

    `table` events (top-level key "table") are a v1 limitation: they
    represent the full current state of a table, not prose or a PMID-style
    citation, and this supplementary-text integration has no rendering for
    them. They're intentionally and silently ignored here — no crash, no
    attempt to flatten tabular content into text, nothing added to
    `citations` either. A future iteration could add real table rendering.
    """
    text_parts: List[str] = []
    citations_by_key: Dict[str, OpenEvidenceCitation] = {}

    for event in events:
        if "table" in event:
            continue

        delta = event.get("text")
        if delta:
            text_parts.append(delta)

        citation = _citation_from_event(event)
        if citation is not None:
            existing = citations_by_key.get(citation.citation_key)
            if existing is None:
                citations_by_key[citation.citation_key] = citation
            else:
                merged_source_texts = list(
                    dict.fromkeys(existing.source_texts + citation.source_texts)
                )
                citations_by_key[citation.citation_key] = existing.model_copy(
                    update={"source_texts": merged_source_texts}
                )

    return OpenEvidenceAnalysis(
        question=question,
        text=_strip_generation_step_prefix("".join(text_parts)),
        citations=list(citations_by_key.values()),
    )


# Domains that identify a citation as a clinical practice guideline rather
# than a journal article.
_GUIDELINE_URL_DOMAINS = ("nccn.org", "asco.org", "esmo.org")

# Non-exhaustive seed list of well-known trial acronyms — a sentence naming
# one of these OR an outcome statistic (PFS/OS/HR/ORR/DFS) is surfaced as a
# trial mention even without a recognized acronym, so this list only needs
# to catch named trials that don't otherwise report a statistic in the same
# sentence.
_KNOWN_TRIAL_ACRONYMS = (
    "ALEX", "FLAURA", "ADAURA", "CROWN", "J-ALEX", "ALTA", "ALTA-1L",
    "ASCEND", "PROFILE", "PALOMA", "MONALEESA", "KEYNOTE", "CHECKMATE",
    "IMPOWER", "OAK", "FLEX", "eXalt3", "LIBRETTO", "ARROW",
)
_TRIAL_ACRONYM_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in _KNOWN_TRIAL_ACRONYMS) + r")\b"
)
_OUTCOME_STAT_PATTERN = re.compile(r"\b(PFS|OS|HR|ORR|DFS)\b")
_CITATION_MARKER_PATTERN = re.compile(r"\[\[\d+\]\]")
_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> List[str]:
    cleaned = _CITATION_MARKER_PATTERN.sub("", text)
    return [sentence.strip() for sentence in _SENTENCE_SPLIT_PATTERN.split(cleaned) if sentence.strip()]


def _extract_guidelines(citations: List[OpenEvidenceCitation]) -> List[OpenEvidenceGuideline]:
    """Clinical practice guideline references — citations whose URL points
    at NCCN, ASCO, or ESMO."""
    guidelines: List[OpenEvidenceGuideline] = []
    for citation in citations:
        url = citation.url or ""
        if not url or not any(domain in url.lower() for domain in _GUIDELINE_URL_DOMAINS):
            continue
        page_anchor = url.split("#", 1)[1] if "#" in url else None
        guidelines.append(
            OpenEvidenceGuideline(
                title=citation.title or citation.citation_key,
                url=url,
                page_anchor=page_anchor or None,
            )
        )
    return guidelines


def _extract_trial_mentions(text: str) -> List[OpenEvidenceTrialMention]:
    """Sentences naming a known clinical trial acronym or an outcome
    statistic (PFS/OS/HR/ORR/DFS)."""
    mentions: List[OpenEvidenceTrialMention] = []
    for sentence in _split_sentences(text):
        trial_match = _TRIAL_ACRONYM_PATTERN.search(sentence)
        if trial_match is None and _OUTCOME_STAT_PATTERN.search(sentence) is None:
            continue
        mentions.append(
            OpenEvidenceTrialMention(
                trial=trial_match.group(1) if trial_match else None,
                sentence=sentence,
            )
        )
    return mentions


def distill_openevidence(analysis: OpenEvidenceAnalysis) -> DistilledOpenEvidence:
    """Deterministically extract the pieces of an OpenEvidenceAnalysis worth
    surfacing as an independent, non-blocking clinical reference card:
    clinical practice guideline references, trial/outcome-statistic
    sentences, and the opening summary sentence (`consensus_role`) — now a
    guideline/trial-recommendation lead-in rather than an oncogene/tumor-
    suppressor classification statement, since _build_question no longer
    asks for the latter (our own synthesis already derives it from retrieved
    abstracts). Purely rule-based (regex over analysis.text/citations, no
    LLM call) — safe to run synchronously inside the sidecar endpoint's
    request path (see GET /v1/genes/{gene}/openevidence in main.py).
    """
    sentences = _split_sentences(analysis.text)
    return DistilledOpenEvidence(
        question=analysis.question,
        consensus_role=sentences[0] if sentences else None,
        guidelines=_extract_guidelines(analysis.citations),
        trial_mentions=_extract_trial_mentions(analysis.text),
        citation_count=len(analysis.citations),
    )


# Domains that identify a citation as coming from a trial registry rather
# than a journal article. A trial-registry record (a protocol/results entry,
# not a journal article) genuinely cannot appear in a PubMed-abstract search
# no matter what — unlike a clinical practice guideline (see
# _GUIDELINE_ORGANIZATION_NAMES below), which our own retrieval treats as
# always-additive by POLICY rather than because it's unreachable; guidelines
# CAN be PubMed-indexed, as the ASCO Living Guideline example there shows.
_TRIAL_REGISTRY_URL_DOMAINS = ("clinicaltrials.gov",)

# A citation whose URL matches any of these domains is "non-PubMed-sourced":
# real-world OpenEvidence citation samples (benchmarks/results/
# openevidence_pointed_20260908/enabled.json) show every other citation —
# regardless of whether its URL happens to be a pubmed.ncbi.nlm.nih.gov link,
# a doi.org redirect, or a publisher site like nejm.org/wiley — is a
# PubMed-indexable journal article: the kind of content our own retrieval
# could in principle have found, even if it happened not to for this
# specific paper.
#
# Deliberately NOT included here: ascopubs.org (ASCO's general journal
# publishing platform, hosting ALL of JCO/JCO-OP/etc., guideline or not). A
# real, ordinary, non-guideline PHAROS-study report (benchmarks/results/
# openevidence_live_20260904/enabled.json, citation_key "35") is published
# in the same journal as the ASCO Living Guideline below and would just as
# plausibly be linked via an ascopubs.org URL instead of a
# pubmed.ncbi.nlm.nih.gov one — trusting that domain unconditionally would
# misclassify it as "always additive" too. See
# is_non_pubmed_sourced_citation's docstring for the narrower, title/author
# based signals used instead for a guideline hosted off this domain list.
_NON_PUBMED_SOURCE_URL_DOMAINS = _GUIDELINE_URL_DOMAINS + _TRIAL_REGISTRY_URL_DOMAINS

# Matches a PMID out of a pubmed.ncbi.nlm.nih.gov citation URL, e.g.
# "https://pubmed.ncbi.nlm.nih.gov/38211832" -> "38211832". This is the only
# reliable, regex-extractable PMID signal on OpenEvidenceCitation today (no
# dedicated pmid field exists on the model).
_PUBMED_CITATION_URL_PATTERN = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")

_TITLE_NORMALIZATION_PATTERN = re.compile(r"[^a-z0-9]+")

# citation.authors exactly equal to a guideline-issuing organization's full
# name — the real NCCN citation shape (authors="National Comprehensive
# Cancer Network", no individual names at all) already relies on this
# alongside its nccn.org domain. Exact-match (not substring) on purpose: a
# real paper's author LIST could plausibly mention one of these
# organizations among several individual co-authors or an acknowledgment,
# which is not the same as the citation's sole "author" BEING the
# organization itself.
_GUIDELINE_ISSUING_ORGANIZATIONS = frozenset(
    {
        "national comprehensive cancer network",
        "american society of clinical oncology",
        "european society for medical oncology",
    }
)

# A clinical practice guideline can be published as an ordinary journal
# article and therefore linked exactly like any other paper — a
# live-captured citation (benchmarks/results/openevidence_live_20260904/
# enabled.json, citation_key "16": "Therapy for Stage IV Non-Small-Cell Lung
# Cancer With Driver Alterations: ASCO Living Guideline", authors "Singh N,
# Temin S, Baker S, et al." — individual names, so the organization-authors
# signal above doesn't catch it either — journal "Journal of Clinical
# Oncology", url "https://pubmed.ncbi.nlm.nih.gov/35816666") has a
# pubmed.ncbi.nlm.nih.gov URL indistinguishable BY DOMAIN from an ordinary
# JCO clinical trial report (that same file's citation_key "35", a
# PHAROS-study report, same journal, same URL shape).
#
# A bare "guideline" substring anywhere in the title is NOT specific enough
# to fix this: a real cohort-validation research PAPER (same file,
# citation_key "26": "Validation of the 5th edition of the World Health
# Organization and International Consensus Classification guidelines for
# TP53-mutated myeloid neoplasm in an independent international cohort") is
# ordinary PubMed-indexed research ABOUT classification guidelines, not
# itself an NCCN/ASCO/ESMO clinical practice guideline, yet contains the
# word "guidelines" too. What actually distinguishes the two: citation "16"
# names a specific guideline-issuing society (ASCO/NCCN/ESMO) directly
# adjacent to "Guideline(s)" as the title's own trailing self-description
# ("...: ASCO Living Guideline"), whereas citation "26"'s "guidelines"
# appears mid-title, generically, with no society name anywhere near it (or
# in the title at all). _GUIDELINE_TITLE_PATTERN requires both: one of the
# three society names, followed (within a short span, to allow a qualifier
# like "Living") by "Guideline"/"Guidelines" anchored at the end of the
# title.
_GUIDELINE_ORGANIZATION_NAMES = ("ASCO", "NCCN", "ESMO")
_GUIDELINE_TITLE_PATTERN = re.compile(
    r"\b(?:" + "|".join(_GUIDELINE_ORGANIZATION_NAMES) + r")\b[\w\s-]{0,40}\bGuidelines?\b\s*\Z",
    re.IGNORECASE,
)


def _normalize_title_for_matching(title: str) -> str:
    """Lowercase and strip all non-alphanumeric characters so titles that
    differ only in punctuation/whitespace/case still compare equal."""
    return _TITLE_NORMALIZATION_PATTERN.sub("", title.lower())


def is_non_pubmed_sourced_citation(citation: OpenEvidenceCitation) -> bool:
    """Whether `citation` is a clinical practice guideline (NCCN/ASCO/ESMO)
    or trial-registry citation, kept always-additive as a deliberate POLICY
    regardless of whether it happens to be PubMed-indexed — not a claim
    that this content type can never appear in PubMed (it can: a guideline
    published as a journal article, like the ASCO Living Guideline example
    below, gets its own PMID like any other paper). The policy exists
    because our own retrieval is PubMed-*abstract*-only and, more
    fundamentally, isn't oriented to surface a document AS a practice
    guideline even on the rare occasion it happens to retrieve one — so a
    curator gets no equivalent "this is the current guideline" signal from
    it either way.

    Checks three independent signals, any of which is enough: the URL
    domain (_NON_PUBMED_SOURCE_URL_DOMAINS), the authors field being
    exactly a guideline-issuing organization's name
    (_GUIDELINE_ISSUING_ORGANIZATIONS), or the title naming a specific
    guideline-issuing society directly adjacent to "Guideline(s)"
    (_GUIDELINE_TITLE_PATTERN). See that pattern's docstring for the real,
    live-captured citations (an ASCO Living Guideline vs. a same-journal
    trial report vs. an unrelated paper that merely discusses guidelines)
    that separate a specific, anchored signal from an over-broad one.
    """
    url = (citation.url or "").lower()
    if url and any(domain in url for domain in _NON_PUBMED_SOURCE_URL_DOMAINS):
        return True
    if (citation.authors or "").strip().lower() in _GUIDELINE_ISSUING_ORGANIZATIONS:
        return True
    return bool(_GUIDELINE_TITLE_PATTERN.search((citation.title or "").strip()))


def _citation_overlaps_core_pipeline_evidence(
    citation: OpenEvidenceCitation, core_pmids: frozenset, core_titles: frozenset
) -> bool:
    """Whether `citation` strongly matches something the core pipeline's own
    GeneAnnotation already surfaced (its verified PMID citations or
    evidence_cards), by PMID (extracted from a pubmed.ncbi.nlm.nih.gov URL)
    or by normalized title. Either signal alone is enough — OpenEvidence's
    citation_key is not a PMID, so PMID is only recoverable from the URL,
    and not every citation has a pubmed.ncbi.nlm.nih.gov-shaped URL even when
    it is the same paper the core pipeline already cited via PMID."""
    match = _PUBMED_CITATION_URL_PATTERN.search(citation.url or "")
    if match is not None and match.group(1) in core_pmids:
        return True
    normalized_title = _normalize_title_for_matching(citation.title or "")
    return bool(normalized_title) and normalized_title in core_titles


def is_additive_citation(
    citation: OpenEvidenceCitation, core_pmids: frozenset, core_titles: frozenset
) -> bool:
    """Whether `citation` is worth surfacing in the sidecar at all: a
    guideline/trial-registry citation always is (by policy for a guideline,
    since our retrieval isn't oriented to surface one as such even on the
    rare occasion it's PubMed-indexed; structurally, for a trial-registry
    record, which cannot appear in a PubMed-abstract search at all — see
    is_non_pubmed_sourced_citation's docstring); a PubMed-sourced (journal
    article) citation only is when it doesn't overlap the core pipeline's
    own verified citations/evidence_cards for this gene — otherwise showing
    it adds vendor-call latency for content the curator already has."""
    if is_non_pubmed_sourced_citation(citation):
        return True
    return not _citation_overlaps_core_pipeline_evidence(citation, core_pmids, core_titles)


# Matches one inline citation marker and captures its numeric key, e.g.
# "[[4]]" -> "4". Distinct from _CITATION_MARKER_PATTERN (which only needs
# to detect/strip markers, not read their keys) — this one backs
# _split_sentences_with_citation_keys's marker-to-sentence linkage below.
_CITATION_KEY_PATTERN = re.compile(r"\[\[(\d+)\]\]")

# Splits `analysis.text` into sentences the same way _split_sentences does
# (sentence-ending punctuation followed by whitespace), but WITHOUT
# stripping citation markers first, and treating end-of-string as an
# implicit sentence boundary too (the `\Z` alternative in the `sentence`
# group) so trailing text with no terminal punctuation still becomes its
# own segment instead of vanishing — real OpenEvidence prose is not
# guaranteed to end mid-thought with a period.
#
# Two separate places a marker can appear relative to a sentence, both of
# which _split_sentences_with_citation_keys below reads keys from:
#   1. ANYWHERE inside the `sentence` span itself. Real captured prose
#      (benchmarks/results/) has markers mid-clause between words
#      ("...limited benefit from[[12]][[34]][[40]] subsequent PARPi"),
#      inside parentheses ("...with added platinum–pemetrexed
#      (HR 0.77[[20]]) at the cost of..."), even splitting a single word
#      ("...a matched Japanese registry found sh[[44]]orter 10-year
#      IDFS..."), or directly before the sentence's own terminal
#      punctuation with a preceding space ("ALEX improved PFS [[1]]."). The
#      non-greedy `sentence` group naturally swallows all of these as
#      ordinary characters (none of them contain a [.!?]), so reading keys
#      out of the whole captured span (not a separate sub-group) is what
#      catches them.
#   2. The `trailing_markers` group: a marker run appearing AFTER a
#      sentence's own terminal punctuation but before the next sentence
#      starts — the convention _build_analysis's docstring describes and
#      tests/test_openevidence_sidecar.py's _ALK_ANALYSIS fixture uses:
#      "...median PFS of 34.8 months versus 10.9 months for crizotinib.
#      [[4]] NCCN guidelines recommend...". Case 1 alone would NOT catch
#      this, since "[[4]]" falls after the sentence's own period, i.e.
#      outside the `sentence` group's captured span.
_SENTENCE_WITH_CITATION_KEYS_PATTERN = re.compile(
    r"(?P<sentence>.+?(?:[.!?]|\Z))(?P<trailing_markers>(?:\s*\[\[\d+\]\])*)(?:\s+|\Z)",
    re.DOTALL,
)


def _split_sentences_with_citation_keys(text: str) -> List[Tuple[str, List[str]]]:
    """Like _split_sentences, but pairs each sentence with the citation
    key(s) (e.g. ["4"]) associated with it — either embedded anywhere
    within its own span, or trailing immediately after it — in the
    original, unstripped text, instead of discarding that association (see
    _SENTENCE_WITH_CITATION_KEYS_PATTERN's docstring for both cases). Used
    only by the additive trial-mention filter below
    (_filter_additive_trial_mentions) — _split_sentences and
    _extract_trial_mentions themselves are unchanged, so a sentence with no
    marker at all (can't be linked to any citation) is simply reported with
    an empty key list rather than dropped."""
    pairs: List[Tuple[str, List[str]]] = []
    for match in _SENTENCE_WITH_CITATION_KEYS_PATTERN.finditer(text):
        raw_sentence = match.group("sentence")
        trailing_markers = match.group("trailing_markers") or ""
        keys = _CITATION_KEY_PATTERN.findall(raw_sentence) + _CITATION_KEY_PATTERN.findall(trailing_markers)
        cleaned_sentence = _CITATION_MARKER_PATTERN.sub("", raw_sentence).strip()
        if not cleaned_sentence:
            continue
        pairs.append((cleaned_sentence, keys))
    return pairs


def _filter_additive_trial_mentions(
    analysis: OpenEvidenceAnalysis, redundant_citation_keys: frozenset
) -> List[OpenEvidenceTrialMention]:
    """Drop a trial/outcome-statistic mention only when EVERY citation
    marker backing its sentence was itself dropped as redundant with the
    core pipeline's own evidence — i.e. the mention purely restates a paper
    the curator already has, the exact leak this function closes (a trial
    stat sentence describing the same dropped PubMed paper as a citation
    would otherwise still surface unfiltered). A mention with no citation
    marker at all is always kept (nothing to attribute it to, so no basis
    to call it redundant), and a mention backed by even one still-additive
    marker (a guideline/trial-registry citation, which is always additive,
    or a non-redundant PubMed one) is always kept too — see
    tests/test_openevidence_sidecar.py for real fixture-grounded cases of
    each: a mention solely backed by a redundant citation (dropped), one
    with no marker at all (kept), and one backed by a guideline citation
    (kept).

    Duplicates _extract_trial_mentions's acronym/outcome-statistic matching
    rather than calling it, because that function consumes
    _split_sentences's marker-STRIPPED sentences and this one needs the
    marker-preserving _split_sentences_with_citation_keys instead.
    """
    mentions: List[OpenEvidenceTrialMention] = []
    for sentence, keys in _split_sentences_with_citation_keys(analysis.text):
        trial_match = _TRIAL_ACRONYM_PATTERN.search(sentence)
        if trial_match is None and _OUTCOME_STAT_PATTERN.search(sentence) is None:
            continue
        if keys and all(key in redundant_citation_keys for key in keys):
            continue
        mentions.append(
            OpenEvidenceTrialMention(
                trial=trial_match.group(1) if trial_match else None,
                sentence=sentence,
            )
        )
    return mentions


def distill_additive_openevidence(
    analysis: OpenEvidenceAnalysis,
    core_pmids: Optional[List[str]] = None,
    core_titles: Optional[List[str]] = None,
) -> DistilledOpenEvidence:
    """Like distill_openevidence, but first drops citations that are
    redundant with the core pipeline's own PubMed-derived evidence for this
    gene — `core_pmids` (GeneAnnotation.citations) and `core_titles`
    (GeneAnnotation.evidence_cards titles), passed in by the caller (see
    GET /v1/genes/{gene}/openevidence in main.py, which has the
    already-computed GeneAnnotation in hand for the same gene).

    Affects `guidelines` and `citation_count` (both derived from
    analysis.citations), the new `redundant_citation_count` (how many
    citations were dropped), and `trial_mentions`: a mention is also
    dropped when every citation marker backing its sentence was dropped as
    redundant — see _filter_additive_trial_mentions's docstring for why
    (otherwise a trial-outcome sentence describing the very paper just
    dropped as a citation would leak the same information back in
    unfiltered). `consensus_role` is derived from analysis.text with no
    citation-marker linkage at all, so is unaffected — see
    is_additive_citation's docstring for why a guideline/trial-registry
    citation is always additive regardless of overlap.
    """
    normalized_pmids = frozenset(pmid.strip() for pmid in (core_pmids or []) if pmid and pmid.strip())
    normalized_titles = frozenset(
        _normalize_title_for_matching(title) for title in (core_titles or []) if title and title.strip()
    )
    additive_citations = []
    redundant_citation_keys = set()
    for citation in analysis.citations:
        if is_additive_citation(citation, normalized_pmids, normalized_titles):
            additive_citations.append(citation)
        else:
            redundant_citation_keys.add(citation.citation_key)
    filtered_analysis = analysis.model_copy(update={"citations": additive_citations})
    distilled = distill_openevidence(filtered_analysis)
    additive_trial_mentions = _filter_additive_trial_mentions(analysis, frozenset(redundant_citation_keys))
    return distilled.model_copy(
        update={
            "redundant_citation_count": len(redundant_citation_keys),
            "trial_mentions": additive_trial_mentions,
        }
    )


def distilled_openevidence_has_additive_content(distilled: DistilledOpenEvidence) -> bool:
    """Whether `distilled` has anything genuinely worth rendering as its own
    card: a guideline reference, a trial/outcome-statistic mention, or at
    least one additive (non-redundant) citation. Used by the sidecar
    endpoint to return {"available": false} instead of a card with nothing
    in it — see distill_additive_openevidence."""
    return bool(distilled.guidelines or distilled.trial_mentions or distilled.citation_count)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_transient_openevidence_error),
)
async def _post_streaming_analysis(question: str, api_key: str, client: httpx.AsyncClient) -> str:
    base_url = settings.openevidence_base_url.strip().rstrip("/")
    url = f"{base_url}{STREAMING_ANALYSIS_PATH}"
    headers = {
        "Authorization": f"Token {api_key}",
        "Accept": "text/event-stream",
    }
    payload = {"text": question, "model": settings.openevidence_model}

    # No [DONE]-style completion sentinel exists in the real API — a stream
    # that ends because the connection was dropped/reset mid-response raises
    # from within aiter_text() itself (an httpx transport exception), which
    # the retry predicate above already treats as transient. A stream that
    # finishes this loop without raising is, by definition, complete.
    async with client.stream(
        "POST", url, json=payload, headers=headers, timeout=settings.openevidence_timeout_seconds
    ) as response:
        response.raise_for_status()
        chunks = [chunk async for chunk in response.aiter_text()]
    return "".join(chunks)


class OpenEvidenceClient:
    """OpenEvidence streaming-analysis client with an explicit shared cache."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key if api_key is not None else settings.openevidence_api_key

    async def get_gene_analysis(
        self,
        gene: str,
        tumor_type: Optional[str] = None,
        fusion: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> OpenEvidenceAnalysis:
        """Return a supplementary, unverified OpenEvidence analysis for `gene`.

        `fusion`, when the gene is part of a fusion (a raw "GENE1::GENE2"
        input string — passed through by the sidecar endpoint's `fusion`
        query param, or derived per gene by openevidence_warmup), makes the
        question fusion-specific via _build_question instead of asking about
        `gene` in isolation.

        A genuine cache hit is returned regardless of whether an API key is
        configured — a Redis cache entry existing does not depend on THIS
        process being able to make a new live call (it may have been warmed
        by benchmarks/warm_openevidence_cache.py, or by a different process
        that had a key configured). OpenEvidenceConfigurationError is only
        raised on an actual cache MISS, when a live HTTP call is genuinely
        about to be made and therefore genuinely needs a key. Pass a shared
        httpx.AsyncClient (e.g. for tests) or one will be created and closed
        for this call.

        The cache key also derives from `fusion` (see _cache_key) whenever
        it resolves to two real partner genes — the fusion-specific question
        built above is a different question than the gene-only one, so it
        must live in a different cache slot, or a fusion-specific answer and
        a gene-only answer for the same gene/tumor_type would silently
        overwrite/return each other.
        """
        question = _build_question(gene, tumor_type, fusion=fusion)
        cache_key = _cache_key(gene, tumor_type, fusion=fusion)

        async def _compute() -> dict:
            # Only reached on a cache miss (cached_call checks Redis first) —
            # this is where a live call is genuinely about to happen, so
            # this is where the API key actually matters.
            if not self.api_key:
                raise OpenEvidenceConfigurationError(
                    "OPENEVIDENCE_API_KEY is required when OPENEVIDENCE_ENABLED is true"
                )
            if client is not None:
                raw = await _post_streaming_analysis(question, self.api_key, client)
            else:
                async with httpx.AsyncClient() as owned_client:
                    raw = await _post_streaming_analysis(question, self.api_key, owned_client)
            events = _parse_sse_events(raw)
            return _build_analysis(question, events).model_dump()

        try:
            payload = await cached_call(
                cache_key, _compute, ttl_seconds=settings.openevidence_cache_ttl_seconds
            )
        except (httpx.HTTPError, RetryError, OpenEvidenceConfigurationError) as exc:
            # cached_call only caches a successful compute() result (see
            # src.pipeline.cache) — an exception here (including a
            # retry-exhausted transient failure, a fail-fast timeout, or a
            # missing-API-key cache miss) is never cached.
            logger.error("OpenEvidence lookup failed for %s: %s", gene, exc)
            raise
        return OpenEvidenceAnalysis(**payload)
