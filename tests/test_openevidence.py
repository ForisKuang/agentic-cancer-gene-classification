"""Tests for the OpenEvidence supplementary evidence client.

No test in this file makes a real network call to openevidence.com — all
HTTP is mocked via httpx.MockTransport, following the pattern used for
OncoKB (tests/test_db_lookups.py) and PubMed (tests/test_literature_cache.py).

Event fixtures below are the REAL, confirmed shapes — verified against both
the official OpenEvidence API docs and a live-captured streaming response
(HTTP 200, real API key, question about BRAF V600E in melanoma). There is no
`[DONE]` sentinel anywhere in the real contract; citation data is nested
under event["reference"]["reference_detail"], not flat top-level fields.
"""

from __future__ import annotations

import json

import httpx
import pytest
from tenacity import RetryError

from src.config import Settings, settings
from src.models.schema import OpenEvidenceCitation
from src.pipeline import cache as cache_module
from src.pipeline.openevidence import (
    OpenEvidenceClient,
    OpenEvidenceConfigurationError,
    _build_analysis,
    _build_question,
    _cache_key,
    _iter_sse_payloads,
    _parse_sse_events,
)

# Verbatim (real, live-captured) NCCN guideline citation — note the absence
# of doi/journal fields, unlike the journal-article example below.
_NCCN_CITATION_EVENT = (
    '{"text": "[[1]]", "reference": {"citation_key": 1, '
    '"reference_text": "National Comprehensive Cancer Network. Melanoma: Cutaneous.", '
    '"reference_detail": {"title": "Melanoma: Cutaneous", '
    '"authors_string": "National Comprehensive Cancer Network", '
    '"publication_info_string": "Updated 2026-09-02", '
    '"publication_date": "2026-09-02", '
    '"url": "https://www.nccn.org/professionals/physician_gls/pdf/cutaneous_melanoma.pdf#page=77"}, '
    '"source_texts": []}}'
)

# From the official docs: a journal-article citation, showing doi/journal_name/
# journal_short_name/source_texts present (which the NCCN example lacks).
_PSORIASIS_CITATION_EVENT = (
    '{"text": "[[2]]", "reference": {"citation_key": 2, '
    '"reference_text": "Lebwohl M, Ting PT, Koo JY. Psoriasis Treatment: Traditional Therapy. '
    'Annals of the Rheumatic Diseases. 2005;64 Suppl 2:ii83-6. doi:10.1136/ard.2004.030791.", '
    '"reference_detail": {"title": "Psoriasis Treatment: Traditional Therapy", '
    '"authors_string": "Lebwohl M, Ting PT, Koo JY.", '
    '"publication_info_string": "Annals of the Rheumatic Diseases. 2005;64 Suppl 2:ii83-6. doi:10.1136/ard.2004.030791.", '
    '"journal_name": "Annals of the Rheumatic Diseases", '
    '"journal_short_name": "Ann Rheum Dis", '
    '"publication_date": "2005-03-01", '
    '"doi": "10.1136/ard.2004.030791", '
    '"url": "https://pubmed.ncbi.nlm.nih.gov/15708945"}, '
    '"source_texts": ["Even before the recent development of biological agents, a long list '
    'of effective treatments has been available for patients with psoriasis..."]}}'
)

_TABLE_EVENT = '{"table": {"rows": [{"gene": "BRAF", "alteration": "V600E"}]}}'

# A real streaming response has no [DONE] terminator — it just ends.
SSE_STREAM = (
    'data: {"text": "BRAF mutations "}\n\n'
    'data: {"text": "are common in melanoma."}\n\n'
    f"data: {_NCCN_CITATION_EVENT}\n\n"
    f"data: {_PSORIASIS_CITATION_EVENT}\n\n"
    f"data: {_TABLE_EVENT}\n\n"
)


@pytest.fixture
async def _require_redis():
    client = cache_module._get_client()
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable: {exc}")


def test_parse_sse_events_parses_all_events_with_no_done_marker():
    """There is no [DONE] sentinel in the real API — all 5 events parse
    (2 text deltas + 2 citation events + 1 table event), nothing is skipped
    or mistaken for a stream terminator."""
    events = _parse_sse_events(SSE_STREAM)
    assert len(events) == 5


def test_parse_sse_events_skips_malformed_payload():
    raw = 'data: {"text": "ok"}\n\ndata: not-json\n\n'
    events = _parse_sse_events(raw)
    assert events == [{"text": "ok"}]


def test_iter_sse_payloads_joins_multiline_data_with_newline_not_concatenation():
    """Per the SSE spec, multiple `data:` lines within one event block must be
    joined with "\\n" between them, not concatenated directly. Plain
    concatenation ("".join) would merge "first line" and "second line" into
    "first linesecond line", silently dropping the separator that was part of
    the original payload's semantics — which can turn a JSON payload that was
    validly split across physical lines into something that fails to parse
    or parses to the wrong value."""
    raw = "data: first line\ndata: second line\n\n"
    payloads = _iter_sse_payloads(raw)
    assert payloads == ["first line\nsecond line"]


def test_parse_sse_events_reassembles_multiline_json_payload():
    """A single JSON event split across two `data:` lines (e.g. a
    pretty-printed payload) must reassemble into one JSON object via the
    "\\n" join, not into unparseable or merged text via concatenation."""
    raw = 'data: {"text":\ndata: "hello"}\n\n'
    events = _parse_sse_events(raw)
    assert events == [{"text": "hello"}]


def test_build_analysis_accumulates_text_from_all_events_including_citations():
    """Per the official docs, concatenating every event's `text` field
    produces the full analysis text — including citation-bearing events,
    whose `text` is an inline marker like "[[1]]", not just plain message
    events."""
    events = _parse_sse_events(SSE_STREAM)
    analysis = _build_analysis("What about BRAF?", events)

    assert analysis.question == "What about BRAF?"
    assert analysis.text == "BRAF mutations are common in melanoma.[[1]][[2]]"


@pytest.mark.parametrize("split_at", [None, 8, 43, 100])
def test_build_analysis_strips_captured_style_widget_prefix(split_at):
    props = {"steps": [{"title": 'Searching "guidelines" {and trials}', "done": True}]}
    text = "REACTCOMPONENT!:!InlineGenerationStep!:!" + json.dumps(props) + "\n\nEvidence: "
    deltas = [text] if split_at is None else [text[:split_at], text[split_at:]]
    raw = "".join("data: " + json.dumps({"text": delta}) + "\n\n" for delta in deltas)
    analysis = _build_analysis("q", _parse_sse_events(raw + SSE_STREAM))

    assert analysis.text == "Evidence: BRAF mutations are common in melanoma.[[1]][[2]]"
    assert len(analysis.citations) == 2


@pytest.mark.parametrize("text", [
    'REACTCOMPONENT!:!InlineGenerationStep!:!{"steps": [',
    'REACTCOMPONENT!:!OtherWidget!:!{"steps": []}Prose.',
    '  Prose with {braces} and [[1]].',
])
def test_build_analysis_preserves_text_without_valid_widget_prefix(text):
    assert _build_analysis("q", [{"text": text}]).text == text


def test_build_analysis_preserves_reference_level_formatted_citation_fields():
    reference = json.loads(_PSORIASIS_CITATION_EVENT)["reference"]
    reference["publication_info_string"] = "Journal. 2026;1:2. doi:example."
    citation = _build_analysis("q", [{"reference": reference}]).citations[0]

    assert citation.reference_text == reference["reference_text"]
    assert citation.publication_info_string == reference["publication_info_string"]
    assert OpenEvidenceCitation.model_validate(citation.model_dump()) == citation


def test_formatted_citation_fields_default_to_none():
    citation = OpenEvidenceCitation(citation_key="1")
    parsed = _build_analysis("q", [{"reference": {"citation_key": 1}}]).citations[0]
    for value in (citation, parsed):
        assert value.reference_text is None
        assert value.publication_info_string is None


def test_build_analysis_extracts_citations_from_real_nested_shape():
    """Citation fields are nested under event["reference"]["reference_detail"],
    not flat top-level fields — this is the real, confirmed shape."""
    events = _parse_sse_events(SSE_STREAM)
    analysis = _build_analysis("What about BRAF?", events)

    assert len(analysis.citations) == 2
    by_key = {c.citation_key: c for c in analysis.citations}

    nccn = by_key["1"]
    assert nccn.reference_text == json.loads(_NCCN_CITATION_EVENT)["reference"]["reference_text"]
    assert nccn.publication_info_string == "Updated 2026-09-02"
    assert nccn.title == "Melanoma: Cutaneous"
    assert nccn.authors == "National Comprehensive Cancer Network"
    assert nccn.journal == ""  # no journal_name/journal_short_name in this fixture
    assert nccn.date == "2026-09-02"
    assert nccn.doi == ""  # no doi in this fixture
    assert nccn.url == "https://www.nccn.org/professionals/physician_gls/pdf/cutaneous_melanoma.pdf#page=77"
    assert nccn.source_texts == []

    psoriasis = by_key["2"]
    reference = json.loads(_PSORIASIS_CITATION_EVENT)["reference"]
    assert psoriasis.reference_text == reference["reference_text"]
    assert psoriasis.publication_info_string == reference["reference_detail"]["publication_info_string"]
    assert psoriasis.title == "Psoriasis Treatment: Traditional Therapy"
    assert psoriasis.authors == "Lebwohl M, Ting PT, Koo JY."
    assert psoriasis.journal == "Annals of the Rheumatic Diseases"  # journal_name preferred
    assert psoriasis.date == "2005-03-01"
    assert psoriasis.doi == "10.1136/ard.2004.030791"
    assert psoriasis.url == "https://pubmed.ncbi.nlm.nih.gov/15708945"
    assert psoriasis.source_texts == [
        "Even before the recent development of biological agents, a long list "
        "of effective treatments has been available for patients with psoriasis..."
    ]


def test_build_analysis_dedupes_repeated_citation_key_merging_source_texts():
    second_occurrence = (
        '{"text": "[[2]]", "reference": {"citation_key": 2, "reference_text": "x", '
        '"reference_detail": {"title": "Psoriasis Treatment: Traditional Therapy"}, '
        '"source_texts": ["A second supporting passage."]}}'
    )
    events = _parse_sse_events(
        f"data: {_PSORIASIS_CITATION_EVENT}\n\n" f"data: {second_occurrence}\n\n"
    )
    analysis = _build_analysis("q", events)

    assert len(analysis.citations) == 1
    citation = analysis.citations[0]
    assert citation.source_texts == [
        "Even before the recent development of biological agents, a long list "
        "of effective treatments has been available for patients with psoriasis...",
        "A second supporting passage.",
    ]


def test_build_analysis_ignores_table_events_without_crashing():
    """`table` events are an intentional v1 limitation — dropped entirely,
    never crash parsing, never pollute accumulated text or citations."""
    events = _parse_sse_events(
        'data: {"text": "before "}\n\n'
        f"data: {_TABLE_EVENT}\n\n"
        'data: {"text": "after"}\n\n'
    )
    analysis = _build_analysis("q", events)

    assert analysis.text == "before after"
    assert analysis.citations == []


# ---------------------------------------------------------------------------
# _build_question: closed/pointed, gene-type-aware question text (replacing
# the old open-ended "summarize the key clinical and molecular evidence" ask
# — see benchmarks/openevidence_value_report.md on the
# agcg-openevidence-benchmark branch for why that phrasing was a problem).
# ---------------------------------------------------------------------------


def test_build_question_plain_gene_no_tumor_type():
    assert _build_question("TP53") == (
        "What NCCN, ASCO, or ESMO clinical practice guideline recommendations "
        "or clinical trial evidence address targeted therapy for TP53 "
        "alterations in cancer? Cite the specific guideline or trial."
    )


def test_build_question_plain_gene_with_tumor_type():
    """tumor_type replaces the generic "cancer" context rather than being
    appended after it — no awkward "...in cancer in breast cancer?" double-up."""
    assert _build_question("BRCA1", tumor_type="breast cancer") == (
        "What NCCN, ASCO, or ESMO clinical practice guideline recommendations "
        "or clinical trial evidence address targeted therapy for BRCA1 "
        "alterations in breast cancer? Cite the specific guideline or trial."
    )


def test_build_question_fusion_gene():
    """`fusion` is a raw "GENE1::GENE2" input string — the exact shape the
    sidecar endpoint's `fusion` query param (GET /v1/genes/{gene}/openevidence
    in main.py) and openevidence_warmup's per-gene fusion derivation (see
    normalization.is_fusion_input) pass through, not a hand-picked tuple of
    gene names."""
    assert _build_question("ALK", fusion="EML4::ALK") == (
        "What NCCN, ASCO, or ESMO clinical practice guideline recommendations "
        "or clinical trial evidence address targeted therapy for the "
        "EML4::ALK fusion in cancer? Cite the specific guideline or trial."
    )


def test_build_question_fusion_gene_with_tumor_type():
    assert _build_question("ALK", tumor_type="NSCLC", fusion="EML4::ALK") == (
        "What NCCN, ASCO, or ESMO clinical practice guideline recommendations "
        "or clinical trial evidence address targeted therapy for the "
        "EML4::ALK fusion in NSCLC? Cite the specific guideline or trial."
    )


# ---------------------------------------------------------------------------
# _cache_key fusion-awareness: a fusion-specific question (see
# test_build_question_fusion_gene above) is a genuinely different question
# than the plain gene-only one, so it must never collide in the same cache
# slot as the gene-only answer or a different fusion's answer for the same
# gene/tumor_type. This was previously an intentional, documented gap (the
# cache key derived only from gene/tumor_type/model) — see the end-to-end
# regression tests below for the observable consequence.
# ---------------------------------------------------------------------------


def test_settings_default_openevidence_model_is_osler(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENEVIDENCE_MODEL", raising=False)
    monkeypatch.delenv("openevidence_model", raising=False)
    monkeypatch.chdir(tmp_path)  # Ignore any developer .env overrides.
    assert Settings().openevidence_model == "osler"


@pytest.mark.parametrize("fusion", [None, "EML4::ALK"])
def test_cache_key_differs_between_osler_and_darwin(monkeypatch, fusion):
    monkeypatch.setattr(settings, "openevidence_model", "osler")
    osler_key = _cache_key("ALK", tumor_type="NSCLC", fusion=fusion)
    monkeypatch.setattr(settings, "openevidence_model", "darwin")
    darwin_key = _cache_key("ALK", tumor_type="NSCLC", fusion=fusion)
    assert osler_key != darwin_key


def test_cache_key_differs_by_fusion_presence():
    plain = _cache_key("ALK", tumor_type="NSCLC")
    fusion_specific = _cache_key("ALK", tumor_type="NSCLC", fusion="EML4::ALK")
    assert plain != fusion_specific


def test_cache_key_differs_between_distinct_fusions_for_same_gene():
    eml4_alk = _cache_key("ALK", tumor_type="NSCLC", fusion="EML4::ALK")
    tfg_alk = _cache_key("ALK", tumor_type="NSCLC", fusion="TFG::ALK")
    assert eml4_alk != tfg_alk


def test_cache_key_matches_across_equivalent_fusion_separators():
    """"::"-, "--"-, and "/"-delimited notations for the same fusion (see
    normalization.split_fusion) must hash to the same key, not fragment the
    cache by input spelling."""
    double_colon = _cache_key("ALK", fusion="EML4::ALK")
    double_dash = _cache_key("ALK", fusion="EML4--ALK")
    slash = _cache_key("ALK", fusion="EML4/ALK")
    assert double_colon == double_dash == slash


def test_cache_key_unchanged_when_fusion_omitted():
    """No behavior change for existing non-fusion callers/cache entries:
    omitting `fusion` (or passing None) produces the exact same key shape as
    before this fix."""
    assert _cache_key("BRAF", tumor_type="melanoma") == "openevidence:" + json.dumps(
        {"gene": "BRAF", "tumor_type": "melanoma", "model": settings.openevidence_model},
        sort_keys=True,
    )


@pytest.mark.asyncio
async def test_get_gene_analysis_fusion_specific_call_does_not_reuse_plain_gene_cache_entry(
    _require_redis,
):
    """End-to-end regression for the cache-collision bug: a plain gene-only
    call and a fusion-specific call for the same gene/tumor_type must live in
    separate cache slots, so neither silently returns the other's answer."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        if "fusion" in payload["text"] or "EML4::ALK" in payload["text"]:
            return httpx.Response(200, text='data: {"text": "Fusion-specific answer."}\n\n')
        return httpx.Response(200, text='data: {"text": "Plain gene answer."}\n\n')

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        plain = await client.get_gene_analysis("ALK", tumor_type="NSCLC", client=http_client)
        fusion_specific = await client.get_gene_analysis(
            "ALK", tumor_type="NSCLC", fusion="EML4::ALK", client=http_client
        )

    # Two distinct live calls were made — the second was NOT a cache hit on
    # the first's (wrong) entry.
    assert len(requests) == 2
    assert plain.text == "Plain gene answer."
    assert fusion_specific.text == "Fusion-specific answer."
    assert "EML4::ALK" in fusion_specific.question
    assert "EML4::ALK" not in plain.question


@pytest.mark.asyncio
async def test_get_gene_analysis_plain_gene_call_does_not_reuse_fusion_specific_cache_entry(
    _require_redis,
):
    """The reverse ordering of the above: warming the fusion-specific slot
    first must not cause a subsequent plain gene-only call to reuse it.
    Uses a different gene/fusion than the previous test so the two tests'
    cache entries can never collide with each other within a shared Redis
    instance."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        if "CD74::ROS1" in payload["text"]:
            return httpx.Response(200, text='data: {"text": "Fusion-specific answer."}\n\n')
        return httpx.Response(200, text='data: {"text": "Plain gene answer."}\n\n')

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        fusion_specific = await client.get_gene_analysis(
            "ROS1", tumor_type="NSCLC", fusion="CD74::ROS1", client=http_client
        )
        plain = await client.get_gene_analysis("ROS1", tumor_type="NSCLC", client=http_client)

    assert len(requests) == 2
    assert fusion_specific.text == "Fusion-specific answer."
    assert plain.text == "Plain gene answer."


@pytest.mark.asyncio
async def test_get_gene_analysis_sends_fusion_specific_question_in_request_payload():
    """End-to-end: the fusion-aware question actually reaches the outgoing
    HTTP request payload, not just _build_question's return value in
    isolation."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, text=SSE_STREAM)

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await client.get_gene_analysis(
            "ALK", tumor_type="NSCLC", fusion="EML4::ALK", client=http_client
        )

    assert captured["payload"]["text"] == (
        "What NCCN, ASCO, or ESMO clinical practice guideline recommendations "
        "or clinical trial evidence address targeted therapy for the "
        "EML4::ALK fusion in NSCLC? Cite the specific guideline or trial."
    )


@pytest.mark.asyncio
async def test_get_gene_analysis_requires_api_key():
    """On a genuine cache MISS, a live call is about to be made, so an API
    key is genuinely required."""
    client = OpenEvidenceClient(api_key="")
    with pytest.raises(OpenEvidenceConfigurationError):
        await client.get_gene_analysis("BRAF")


@pytest.mark.asyncio
async def test_get_gene_analysis_cache_hit_is_consumable_without_api_key(_require_redis):
    """A genuine Redis cache hit must be returned regardless of whether an
    API key is configured on THIS client instance — a cache entry existing
    does not depend on this process being able to make a NEW live call. It
    may have been warmed by a different process (e.g.
    benchmarks/warm_openevidence_cache.py) that did have a key. Before the
    fix, the API-key check ran before the cache was even consulted, so a
    keyless process could never consume an otherwise-valid cache entry."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=SSE_STREAM)

    # First, warm the cache using a client that DOES have a key.
    warming_client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await warming_client.get_gene_analysis("BRAF", client=http_client)
    assert len(requests) == 1

    # Now a keyless client must still be able to consume that cache entry —
    # no live call, no OpenEvidenceConfigurationError.
    keyless_client = OpenEvidenceClient(api_key="")
    analysis = await keyless_client.get_gene_analysis("BRAF")

    assert analysis.text == "BRAF mutations are common in melanoma.[[1]][[2]]"
    assert len(requests) == 1  # still just the one warming call, no new attempt


@pytest.mark.asyncio
async def test_get_gene_analysis_parses_mocked_stream():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/streaming/analysis"
        assert request.headers["authorization"] == "Token test-key"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(200, text=SSE_STREAM)

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        analysis = await client.get_gene_analysis("BRAF", tumor_type="melanoma", client=http_client)

    assert len(requests) == 1
    assert analysis.text == "BRAF mutations are common in melanoma.[[1]][[2]]"
    assert {c.citation_key for c in analysis.citations} == {"1", "2"}


@pytest.mark.asyncio
async def test_get_gene_analysis_caches_across_calls(_require_redis):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=SSE_STREAM)

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        first = await client.get_gene_analysis("BRAF", client=http_client)
        second = await client.get_gene_analysis("BRAF", client=http_client)

    assert first == second
    assert len(requests) == 1  # second call was a cache hit, no new HTTP request


@pytest.mark.asyncio
async def test_get_gene_analysis_clean_stream_with_no_done_marker_is_cached(_require_redis):
    """Round 4 fix: there is no [DONE] sentinel in the real API (confirmed
    absent from the official docs and a live capture). A stream that simply
    ends normally — the HTTP body finishes, no transport exception — must be
    treated as a complete, valid, CACHEABLE result. (Round 3 had this
    backwards: it required seeing a literal "[DONE]" payload, which would
    have caused every real production call to be treated as incomplete and
    exhaust its retries.)"""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=SSE_STREAM)  # ends cleanly, no [DONE]

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        first = await client.get_gene_analysis("BRAF", client=http_client)
        second = await client.get_gene_analysis("BRAF", client=http_client)

    assert first == second
    assert len(first.citations) == 2
    assert len(requests) == 1  # cached after the first successful (DONE-less) call


@pytest.mark.asyncio
async def test_get_gene_analysis_transport_error_retries_and_is_not_cached(_require_redis):
    """A genuinely dropped/reset connection mid-stream is how an incomplete
    response actually surfaces against the real API — httpx itself raises a
    transport exception, which the retry predicate treats as transient. This
    is the "transport-exception-based incompleteness detection" that
    replaces the old (incorrect) [DONE]-sentinel check."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ReadError("connection reset mid-stream", request=request)

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(RetryError):
            await client.get_gene_analysis("BRAF", client=http_client)

    assert attempts["count"] == 3  # all retry attempts consumed, never succeeded

    cache_key = "openevidence:" + json.dumps(
        {"gene": "BRAF", "tumor_type": "", "model": settings.openevidence_model},
        sort_keys=True,
    )
    cached = await cache_module._get_client().get(cache_key)
    assert cached is None  # nothing was cached — compute() never returned successfully


@pytest.mark.asyncio
async def test_get_gene_analysis_retries_on_transient_failure():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, text=SSE_STREAM)

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        analysis = await client.get_gene_analysis("BRAF", client=http_client)

    assert attempts["count"] == 2
    assert analysis.text == "BRAF mutations are common in melanoma.[[1]][[2]]"


@pytest.mark.asyncio
async def test_get_gene_analysis_does_not_retry_permanent_4xx():
    """A 401 (bad API key) can never succeed on retry — it must fail fast on
    the first attempt rather than burning the retry budget."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(401, text="unauthorized")

    client = OpenEvidenceClient(api_key="bad-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_gene_analysis("BRAF", client=http_client)

    assert attempts["count"] == 1


@pytest.mark.asyncio
async def test_get_gene_analysis_does_not_retry_on_timeout():
    """OpenEvidence's real-world latency can run into minutes for a complex
    question (a live smoke test ran ~220s without finishing). Retrying a
    slow-but-functioning server would only multiply an already multi-minute
    wait for what's meant to be a quick, best-effort lookup — a timeout must
    fail fast: single attempt, no retry."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(httpx.ReadTimeout):
            await client.get_gene_analysis("BRAF", client=http_client)

    assert attempts["count"] == 1
