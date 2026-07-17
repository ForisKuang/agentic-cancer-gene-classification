"""
Two-tier literature retrieval.

Tier 1 (cheap): Direct NCBI E-utilities query with a structured search term.
                Sufficient for well-characterised genes with abundant literature.

Tier 2 (fallback): When Tier 1 returns fewer than MIN_PAPERS, Claude becomes
                   the retriever. It receives a search_pubmed tool and decides
                   what queries to run — trying aliases, fusion-specific terms,
                   pathway names, or any angle it judges relevant — until it
                   signals it has enough evidence.

The fallthrough boundary is settings.min_papers_for_strong_association (default 4).
PMID verification at synthesis time applies regardless of which tier produced the records.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Set

import anthropic
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.models.schema import (
    LiteratureRecord,
    NCBIAPIKeyConfigResponse,
    NCBIConfigStatus,
)
from src.pipeline.local_config import app_config_dir, write_secret_file
from src.pipeline.llm_client import complete_with_tool
from src.pipeline.selection import strongest_selection_score

logger = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

NCBI_API_KEY_FILENAME = "ncbi-api-key.txt"
_request_semaphore_without_key = asyncio.Semaphore(3)
_request_semaphore_with_key = asyncio.Semaphore(10)

MAX_AGENTIC_TOOL_CALLS = 6  # cap Claude's search budget per gene

# ---------------------------------------------------------------------------
# Shared NCBI helpers (used by both tiers)
# ---------------------------------------------------------------------------

def _ncbi_params(extra: dict) -> dict:
    params = {"retmode": "json", **extra}
    api_key = configured_ncbi_api_key()
    if api_key:
        params["api_key"] = api_key
    return params


def _ncbi_rate_limit(api_key: str) -> tuple[float, asyncio.Semaphore]:
    if api_key:
        return 0.11, _request_semaphore_with_key
    return 0.34, _request_semaphore_without_key


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
async def _esearch(query: str, max_results: int, client: httpx.AsyncClient) -> List[str]:
    """Return PMIDs matching a PubMed query string."""
    params = _ncbi_params(
        {"db": "pubmed", "term": query, "retmax": max_results, "sort": "relevance"}
    )
    delay, semaphore = _ncbi_rate_limit(params.get("api_key", ""))
    async with semaphore:
        await asyncio.sleep(delay)
        resp = await client.get(ESEARCH_URL, params=params, timeout=15.0)
        resp.raise_for_status()
    return resp.json().get("esearchresult", {}).get("idlist", [])


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
async def _efetch(pmids: List[str], client: httpx.AsyncClient) -> List[LiteratureRecord]:
    """Fetch abstracts for a list of PMIDs."""
    if not pmids:
        return []
    params = _ncbi_params(
        {"db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "xml"}
    )
    delay, semaphore = _ncbi_rate_limit(params.get("api_key", ""))

    async with semaphore:
        await asyncio.sleep(delay)
        resp = await client.get(EFETCH_URL, params=params, timeout=30.0)
        resp.raise_for_status()

    records: List[LiteratureRecord] = []
    try:
        root = ET.fromstring(resp.text)
        for article in root.findall(".//PubmedArticle"):
            pmid_el = article.find(".//PMID")
            pmid = pmid_el.text if pmid_el is not None else None
            title_el = article.find(".//ArticleTitle")
            title = (title_el.text or "").strip() if title_el is not None else ""
            abstract_parts = article.findall(".//AbstractText")
            abstract = " ".join(
                (el.text or "").strip() for el in abstract_parts if el.text
            ).strip()
            if pmid and abstract:
                records.append(LiteratureRecord(pmid=pmid, title=title, abstract=abstract))
    except ET.ParseError as exc:
        logger.warning("XML parse error in efetch: %s", exc)
    return records


def configured_ncbi_api_key() -> str:
    if settings.ncbi_api_key:
        return settings.ncbi_api_key
    path = default_ncbi_api_key_path()
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def ncbi_config_status() -> NCBIConfigStatus:
    if settings.ncbi_api_key:
        return NCBIConfigStatus(configured=True, source="environment")
    api_key = configured_ncbi_api_key()
    if api_key:
        return NCBIConfigStatus(configured=True, source="local_upload")
    return NCBIConfigStatus(configured=False)


def save_ncbi_api_key(api_key: str) -> NCBIAPIKeyConfigResponse:
    key = api_key.strip()
    if not key:
        raise ValueError("NCBI API key cannot be blank.")
    write_secret_file(default_ncbi_api_key_path(), key + "\n")
    return NCBIAPIKeyConfigResponse(
        configured=True,
        source="local_upload",
        message="NCBI API key is configured for this computer.",
    )


def default_ncbi_api_key_path() -> Path:
    return app_config_dir() / NCBI_API_KEY_FILENAME


async def _search_and_fetch(
    query: str,
    max_results: int,
    client: httpx.AsyncClient,
    already_seen: Set[str],
) -> List[LiteratureRecord]:
    """Run esearch + efetch, skipping PMIDs already in the accumulator."""
    pmids = await _esearch(query, max_results, client)
    new_pmids = [p for p in pmids if p not in already_seen]
    if not new_pmids:
        return []
    return await _efetch(new_pmids, client)


# ---------------------------------------------------------------------------
# Tier 1: parallel multi-query retrieval
# ---------------------------------------------------------------------------

def _fusion_partners(gene: str, fusions: List[str]) -> List[str]:
    """Extract partner symbols from fusion strings, excluding the gene itself."""
    partners: List[str] = []
    for fusion in fusions:
        for sep in ("::", "--", "/"):
            if sep in fusion:
                for part in fusion.split(sep):
                    part = part.strip()
                    if part and part != gene:
                        partners.append(part)
                break
    return list(dict.fromkeys(partners))


async def _tier1_retrieve(
    gene: str,
    fusions: Optional[List[str]] = None,
) -> List[LiteratureRecord]:
    """
    Primary retrieval: runs multiple PubMed queries in parallel to maximise
    coverage before falling through to the agentic Tier 2.

    Queries run concurrently:
      1. Gene Name MeSH field query (high precision)
      2. Free-text broadening query (catches alias spellings)
      3. Co-query with each fusion partner (catches fusion-specific papers)

    Results are deduplicated by PMID and fetched across query families before
    the downstream selection pass narrows the synthesis context.
    """
    queries = [
        f'"{gene}"[Gene Name] AND cancer[MeSH Terms]',
        f'"{gene}" AND (cancer OR tumor OR oncology OR carcinoma)',
    ]
    for partner in _fusion_partners(gene, fusions or [])[:2]:
        queries.append(f'"{gene}" AND "{partner}"')

    async with httpx.AsyncClient() as client:
        pmid_lists = await asyncio.gather(
            *[_esearch(q, settings.pubmed_max_results, client) for q in queries]
        )
        seen: Set[str] = set()
        merged: List[str] = []
        for pmids in pmid_lists:
            for pmid in pmids:
                if pmid not in seen:
                    seen.add(pmid)
                    merged.append(pmid)
        fetch_cap = settings.pubmed_max_results * len(queries)
        records = await _efetch(merged[:fetch_cap], client)

    logger.info(
        "Tier 1: %d abstracts for %s (%d queries, %d unique PMIDs before cap)",
        len(records), gene, len(queries), len(merged),
    )
    return records


# ---------------------------------------------------------------------------
# Tier 2: Claude agentic retrieval
# ---------------------------------------------------------------------------

_SEARCH_PUBMED_TOOL: anthropic.types.ToolParam = {
    "name": "search_pubmed",
    "description": (
        "Search PubMed for papers about a gene or topic in a cancer context. "
        "You may call this multiple times with different queries — aliases, pathway names, "
        "fusion-specific terms, disease contexts — to collect enough evidence. "
        "Call done() when you have sufficient abstracts or have exhausted useful queries."
    ),
    "input_schema": {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "PubMed query string. Examples: '\"DDEFL1\" AND cancer', "
                    "'\"EML4-ALK\" fusion lung adenocarcinoma', "
                    "'\"TRARG1\" OR \"TUSC5\" tumor suppressor'"
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Max abstracts to retrieve (default 10, max 20).",
                "default": 10,
            },
        },
    },
}

_DONE_TOOL: anthropic.types.ToolParam = {
    "name": "done",
    "description": "Signal that you have retrieved sufficient literature and are ready for synthesis.",
    "input_schema": {"type": "object", "properties": {}},
}

_SUGGEST_QUERIES_TOOL: dict = {
    "name": "suggest_pubmed_queries",
    "description": (
        "Return PubMed queries that are likely to find cancer-relevant evidence for this gene."
    ),
    "input_schema": {
        "type": "object",
        "required": ["queries"],
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "PubMed query strings, ordered from most to least promising.",
            }
        },
    },
}

_AGENTIC_SYSTEM = """\
You are a cancer genomics literature specialist. Your job is to find PubMed papers
that establish or refute the cancer relevance of a given gene.

You have a search_pubmed tool. Use it to retrieve abstracts by trying different angles:
- The canonical HUGO symbol and known aliases
- Fusion-specific terms (e.g. "EML4-ALK fusion")
- Associated pathways or protein family terms
- Specific cancer types where this gene is suspected to be relevant

Aim for at least 4 high-quality abstracts with direct cancer relevance. If a gene has
no cancer literature after 3–4 different search attempts, call done() — "insufficient evidence"
is a valid outcome and should not be papered over with loosely-related papers.

Do not fabricate PMIDs or cite papers you did not retrieve via search_pubmed.
"""


def _format_initial_records(records: List[LiteratureRecord]) -> str:
    if not records:
        return "No papers retrieved yet."
    lines = [f"Already retrieved {len(records)} paper(s) from an initial query (insufficient):"]
    for r in records:
        lines.append(f"  PMID {r.pmid}: {r.title[:80]}")
    return "\n".join(lines)


async def _tier2_agentic_retrieve(
    gene: str,
    fusions: List[str],
    initial_records: List[LiteratureRecord],
) -> List[LiteratureRecord]:
    """
    Fallback retrieval: Claude decides what queries to run.
    Runs an agentic tool-use loop, accumulating unique records across all searches.
    """
    accumulated: Dict[str, LiteratureRecord] = {r.pmid: r for r in initial_records}

    fusion_context = f"Associated fusions: {', '.join(fusions)}" if fusions else ""
    initial_summary = _format_initial_records(initial_records)

    user_message = (
        f"Gene: {gene}\n"
        f"{fusion_context}\n\n"
        f"{initial_summary}\n\n"
        f"Please search for additional cancer-relevant literature for {gene}. "
        f"Try different queries including aliases and fusion-specific terms. "
        f"Call done() when satisfied or after exhausting useful approaches."
    )

    messages = [{"role": "user", "content": user_message}]
    tool_calls_made = 0
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async with httpx.AsyncClient() as http_client:
        while tool_calls_made < MAX_AGENTIC_TOOL_CALLS:
            response = await client.messages.create(
                model=settings.synthesis_model,
                max_tokens=1024,
                system=_AGENTIC_SYSTEM,
                tools=[_SEARCH_PUBMED_TOOL, _DONE_TOOL],
                messages=messages,
            )

            # Accumulate assistant turn
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                logger.info("Claude ended agentic retrieval for %s without calling done()", gene)
                break

            # Process all tool_use blocks in this turn
            tool_results = []
            called_done = False

            for block in response.content:
                if block.type != "tool_use":
                    continue

                if block.name == "done":
                    called_done = True
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": "Acknowledged."}
                    )
                    continue

                if block.name == "search_pubmed":
                    if tool_calls_made >= MAX_AGENTIC_TOOL_CALLS:
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": "Search budget exhausted; no more PubMed searches will be run.",
                            }
                        )
                        continue

                    query = block.input.get("query", "")
                    max_res = min(int(block.input.get("max_results", 10)), 20)
                    tool_calls_made += 1
                    logger.info(
                        "Claude search_pubmed [%d/%d] for %s: %s",
                        tool_calls_made, MAX_AGENTIC_TOOL_CALLS, gene, query,
                    )

                    try:
                        new_records = await _search_and_fetch(
                            query, max_res, http_client, set(accumulated.keys())
                        )
                        for r in new_records:
                            accumulated[r.pmid] = r
                        result_text = (
                            f"Found {len(new_records)} new abstracts.\n"
                            + "\n".join(
                                f"PMID {r.pmid}: {r.title[:80]}\n{r.abstract[:200]}..."
                                for r in new_records
                            )
                        ) if new_records else "No new results for this query."
                    except httpx.HTTPError as exc:
                        result_text = f"Search failed: {exc}"

                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": result_text}
                    )

            # Feed all tool results back in a single user turn
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if called_done or response.stop_reason != "tool_use":
                break

    total = len(accumulated)
    logger.info(
        "Tier 2 complete for %s: %d total abstracts (%d from agentic, %d tool calls)",
        gene, total, total - len(initial_records), tool_calls_made,
    )
    return list(accumulated.values())


async def _tier2_local_retrieve(
    gene: str,
    fusions: List[str],
    initial_records: List[LiteratureRecord],
    local_backend: Optional[str],
) -> List[LiteratureRecord]:
    """
    Local fallback retrieval for Claude Code mode.

    Claude Code cannot participate in Anthropic SDK tool-use loops, so ask it for
    concrete PubMed query strings, then execute those queries locally.
    """
    accumulated: Dict[str, LiteratureRecord] = {r.pmid: r for r in initial_records}
    fusion_context = f"Associated fusions: {', '.join(fusions)}" if fusions else "Associated fusions: none"
    initial_summary = _format_initial_records(initial_records)
    user_prompt = (
        f"Gene: {gene}\n"
        f"{fusion_context}\n\n"
        f"{initial_summary}\n\n"
        "Suggest up to 6 PubMed queries to find direct cancer-relevant evidence for this gene. "
        "Include aliases, fusion partner context, pathway or protein-family terms when useful. "
        "Return only queries that should be run against PubMed."
    )

    result = await complete_with_tool(
        model=settings.synthesis_model,
        system=_AGENTIC_SYSTEM,
        user=user_prompt,
        tool=_SUGGEST_QUERIES_TOOL,
        max_tokens=1024,
        local_mode=True,
        local_backend=local_backend,
    )
    queries = [q for q in result.get("queries", []) if isinstance(q, str) and q.strip()]
    queries = list(dict.fromkeys(q.strip() for q in queries))[:MAX_AGENTIC_TOOL_CALLS]

    async with httpx.AsyncClient() as http_client:
        for i, query in enumerate(queries, start=1):
            logger.info(
                "Local agent suggested PubMed query [%d/%d] for %s: %s",
                i,
                len(queries),
                gene,
                query,
            )
            try:
                new_records = await _search_and_fetch(
                    query, 20, http_client, set(accumulated.keys())
                )
            except httpx.HTTPError as exc:
                logger.warning("Local Tier 2 PubMed query failed for %s: %s", gene, exc)
                continue
            for record in new_records:
                accumulated[record.pmid] = record

    logger.info(
        "Local Tier 2 complete for %s: %d total abstracts (%d from suggested queries)",
        gene,
        len(accumulated),
        len(accumulated) - len(initial_records),
    )
    return list(accumulated.values())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def retrieve_literature(
    gene: str,
    fusions: Optional[List[str]] = None,
    local_mode: bool = False,
    local_backend: Optional[str] = None,
) -> tuple:
    """
    Two-tier retrieval with automatic fallthrough.

    Tier 1: direct NCBI query (always runs first, cheap).
    Tier 2: Claude agentic retrieval (only when Tier 1 is insufficient).

    The threshold is settings.min_papers_for_strong_association (default 4).

    Returns (records, tier) where tier is 1 or 2.
    """
    try:
        records = await _tier1_retrieve(gene, fusions)
    except httpx.HTTPError as exc:
        logger.error("Tier 1 NCBI call failed for %s: %s", gene, exc)
        records = []

    if len(records) >= settings.min_papers_for_strong_association:
        logger.info(
            "Tier 1 sufficient for %s (%d papers) — skipping Claude retrieval",
            gene, len(records),
        )
        return records, 1

    logger.info(
        "Tier 1 insufficient for %s (%d < %d papers) — falling through to Claude",
        gene, len(records), settings.min_papers_for_strong_association,
    )
    if local_mode:
        strongest_score = strongest_selection_score(gene, records)
        if strongest_score < settings.local_tier2_min_prefilter_score:
            logger.info(
                "Skipping local Tier 2 for %s: strongest deterministic relevance score %d < %d",
                gene,
                strongest_score,
                settings.local_tier2_min_prefilter_score,
            )
            return records, 1

    if local_mode:
        records = await _tier2_local_retrieve(gene, fusions or [], records, local_backend)
    else:
        records = await _tier2_agentic_retrieve(gene, fusions or [], records)
    return records, 2
