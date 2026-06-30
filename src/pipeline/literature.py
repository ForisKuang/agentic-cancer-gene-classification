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
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Set

import anthropic
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.models.schema import LiteratureRecord

logger = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

_RATE_LIMIT_DELAY = 0.34 if not settings.ncbi_api_key else 0.11
_request_semaphore = asyncio.Semaphore(3 if not settings.ncbi_api_key else 10)

MAX_AGENTIC_TOOL_CALLS = 6  # cap Claude's search budget per gene

# ---------------------------------------------------------------------------
# Shared NCBI helpers (used by both tiers)
# ---------------------------------------------------------------------------

def _ncbi_params(extra: dict) -> dict:
    params = {"retmode": "json", **extra}
    if settings.ncbi_api_key:
        params["api_key"] = settings.ncbi_api_key
    return params


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
async def _esearch(query: str, max_results: int, client: httpx.AsyncClient) -> List[str]:
    """Return PMIDs matching a PubMed query string."""
    params = _ncbi_params(
        {"db": "pubmed", "term": query, "retmax": max_results, "sort": "relevance"}
    )
    async with _request_semaphore:
        await asyncio.sleep(_RATE_LIMIT_DELAY)
        resp = await client.get(ESEARCH_URL, params=params, timeout=15.0)
        resp.raise_for_status()
    return resp.json().get("esearchresult", {}).get("idlist", [])


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
async def _efetch(pmids: List[str], client: httpx.AsyncClient) -> List[LiteratureRecord]:
    """Fetch abstracts for a list of PMIDs."""
    if not pmids:
        return []
    params = {"db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "xml"}
    if settings.ncbi_api_key:
        params["api_key"] = settings.ncbi_api_key

    async with _request_semaphore:
        await asyncio.sleep(_RATE_LIMIT_DELAY)
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
# Tier 1: direct structured query
# ---------------------------------------------------------------------------

async def _tier1_retrieve(gene: str) -> List[LiteratureRecord]:
    """
    Primary retrieval: one structured PubMed query for the gene in a cancer context.
    Fast and cheap — handles the majority of well-characterised genes.
    """
    query = f'"{gene}"[Gene Name] AND cancer[MeSH Terms]'
    async with httpx.AsyncClient() as client:
        pmids = await _esearch(query, settings.pubmed_max_results, client)
        if not pmids:
            # Broaden if MeSH term yields nothing
            query = f'"{gene}" AND (cancer OR tumor OR oncology OR carcinoma)'
            pmids = await _esearch(query, settings.pubmed_max_results, client)
        records = await _efetch(pmids, client)
    logger.info("Tier 1: %d abstracts for %s", len(records), gene)
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


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def retrieve_literature(
    gene: str,
    fusions: Optional[List[str]] = None,
) -> tuple:
    """
    Two-tier retrieval with automatic fallthrough.

    Tier 1: direct NCBI query (always runs first, cheap).
    Tier 2: Claude agentic retrieval (only when Tier 1 is insufficient).

    The threshold is settings.min_papers_for_strong_association (default 4).

    Returns (records, tier) where tier is 1 or 2.
    """
    try:
        records = await _tier1_retrieve(gene)
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
    records = await _tier2_agentic_retrieve(gene, fusions or [], records)
    return records, 2
