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
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Set

import anthropic
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.models.schema import LiteratureRecord
from src.pipeline.llm_client import complete_with_tool, make_async_sdk_client, resolve_sdk_model

logger = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

_RATE_LIMIT_DELAY = 0.34 if not settings.ncbi_api_key else 0.11
_request_semaphore = asyncio.Semaphore(3 if not settings.ncbi_api_key else 10)

MAX_AGENTIC_TOOL_CALLS = 6  # cap Claude's search budget per gene

# ---------------------------------------------------------------------------
# Tumor type alias expansion
# ---------------------------------------------------------------------------

_TUMOR_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    # NSCLC subtypes
    "luad": ("lung adenocarcinoma", "LUAD"),
    "lusc": ("lung squamous cell carcinoma", "LUSC"),
    "nsclc": ("non-small cell lung cancer", "NSCLC"),
    "non-small cell lung cancer": ("non-small cell lung cancer", "NSCLC"),
    "sclc": ("small cell lung cancer", "SCLC"),
    # Breast
    "tnbc": ("triple-negative breast cancer", "TNBC"),
    "invasive breast carcinoma": ("breast cancer", "breast carcinoma", "invasive breast carcinoma"),
    # GI
    "crc": ("colorectal cancer", "CRC", "colon cancer"),
    "coad": ("colon adenocarcinoma", "colorectal cancer", "COAD"),
    "read": ("rectal adenocarcinoma", "rectal cancer", "READ"),
    "colorectal adenocarcinoma": ("colorectal cancer", "colon cancer", "rectal cancer", "CRC"),
    "pdac": ("pancreatic ductal adenocarcinoma", "PDAC", "pancreatic cancer"),
    "pancreatic adenocarcinoma": ("pancreatic cancer", "PDAC", "pancreatic ductal adenocarcinoma"),
    "hcc": ("hepatocellular carcinoma", "HCC", "liver cancer"),
    "gc": ("gastric cancer", "stomach cancer"),
    "stad": ("stomach adenocarcinoma", "gastric cancer", "STAD"),
    "eac": ("esophageal adenocarcinoma", "EAC"),
    "escc": ("esophageal squamous cell carcinoma", "ESCC"),
    "esophagogastric cancer": (
        "esophageal cancer", "gastroesophageal cancer", "esophagogastric", "gastric cancer",
    ),
    "cca": ("cholangiocarcinoma", "bile duct cancer"),
    # Renal
    "rcc": ("renal cell carcinoma", "RCC", "kidney cancer"),
    "ccrcc": ("clear cell renal cell carcinoma", "ccRCC"),
    "kirc": ("kidney renal clear cell carcinoma", "clear cell RCC"),
    "kirp": ("kidney renal papillary cell carcinoma", "papillary RCC"),
    # Brain / CNS
    "gbm": ("glioblastoma", "GBM", "glioblastoma multiforme"),
    "lgg": ("lower grade glioma", "LGG", "glioma"),
    # Hematologic malignancies
    "aml": ("acute myeloid leukemia", "AML"),
    "cml": ("chronic myeloid leukemia", "CML"),
    "all": ("acute lymphoblastic leukemia", "ALL", "acute lymphocytic leukemia"),
    "cll": ("chronic lymphocytic leukemia", "CLL"),
    "mm": ("multiple myeloma", "MM"),
    "dlbcl": ("diffuse large B-cell lymphoma", "DLBCL"),
    "fl": ("follicular lymphoma", "FL"),
    "mcl": ("mantle cell lymphoma", "MCL"),
    "nhl": ("non-Hodgkin lymphoma", "NHL"),
    "hl": ("Hodgkin lymphoma", "HL", "Hodgkin disease"),
    # Gynecologic
    "ov": ("ovarian cancer", "ovarian serous carcinoma", "OV"),
    "ucec": ("uterine corpus endometrial carcinoma", "endometrial cancer", "UCEC"),
    "cesc": ("cervical squamous cell carcinoma", "cervical cancer", "CESC"),
    # Skin
    "skcm": ("skin cutaneous melanoma", "melanoma", "SKCM"),
    "uvm": ("uveal melanoma", "UVM"),
    # Head and neck
    "hnscc": ("head and neck squamous cell carcinoma", "HNSCC"),
    # Thyroid
    "thca": ("thyroid carcinoma", "THCA", "thyroid cancer"),
    # Prostate
    "prad": ("prostate adenocarcinoma", "prostate cancer", "PRAD"),
    # Bladder
    "blca": ("bladder urothelial carcinoma", "bladder cancer", "BLCA"),
    "uc": ("urothelial carcinoma", "bladder cancer"),
    "urothelial carcinoma": ("urothelial carcinoma", "bladder cancer", "UC"),
    # Other
    "meso": ("mesothelioma", "MESO"),
    "sarc": ("sarcoma", "SARC"),
    "lcnec": ("large cell neuroendocrine carcinoma", "LCNEC"),
    "pcpg": ("pheochromocytoma", "paraganglioma", "PCPG"),
}


def _expand_tumor_type_terms(tumor_type: str) -> list[str]:
    """Return PubMed search phrases for tumor_type, expanding abbreviations/aliases."""
    aliases = _TUMOR_TYPE_ALIASES.get(tumor_type.strip().lower())
    return list(aliases) if aliases else [tumor_type.strip()]


def _tumor_type_query_fragment(tumor_type: str) -> str:
    """Build a quoted OR-clause covering tumor_type and all its known aliases."""
    terms = _expand_tumor_type_terms(tumor_type)
    if len(terms) == 1:
        return f'"{terms[0]}"'
    return "(" + " OR ".join(f'"{t}"' for t in terms) + ")"


def _tumor_type_prompt_note(tumor_type: str) -> str:
    """Human-readable tumor type hint for LLM prompts, listing known aliases."""
    expanded = _expand_tumor_type_terms(tumor_type)
    if len(expanded) > 1:
        return f"{tumor_type} (also search: {', '.join(expanded)})"
    return tumor_type


# ---------------------------------------------------------------------------
# Evidence priority ranking
# ---------------------------------------------------------------------------

_HIGH_IMPACT_JOURNALS: frozenset[str] = frozenset({
    "N Engl J Med", "Lancet", "JAMA", "BMJ", "Nat Med", "Nat Genet",
    "Nature", "Science", "Cell", "Nat Cancer", "Nat Commun",
    "Sci Transl Med", "EMBO J",
    "J Clin Oncol", "Cancer Cell", "Cancer Discov", "Cancer Res",
    "Clin Cancer Res", "Ann Oncol", "Lancet Oncol", "JAMA Oncol",
    "Mol Cancer Ther", "Mol Cancer", "Oncogene", "Cancer Lett",
    "Blood", "Leukemia", "J Hematol Oncol", "Haematologica",
    "Genome Res", "Genome Biol", "Nucleic Acids Res", "Nat Biotechnol",
})

_HUMAN_CLINICAL_TERMS: tuple[str, ...] = (
    "patient", "clinical trial", "cohort", "phase i", "phase ii", "phase iii",
    "randomized", "overall survival", "progression-free", "response rate",
    "tumor biopsy", "case report", "case series",
)
_CELL_LINE_TERMS: tuple[str, ...] = (
    "cell line", "in vitro", "xenograft", "knockdown", "knockout", "overexpression",
    "proliferation", "apoptosis", "transfection", "shRNA", "siRNA", "CRISPR",
)
_MOUSE_MODEL_TERMS: tuple[str, ...] = (
    "mouse model", "transgenic", "in vivo", "tumor formation", "tumor growth",
    "orthotopic", "syngeneic", "genetically engineered",
)


def _record_text(record: LiteratureRecord) -> str:
    return " ".join((record.title, record.abstract, " ".join(record.publication_types))).lower()


def _contains_phrase(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text) is not None


def _publication_evidence_rank(record: LiteratureRecord) -> int:
    """0=human clinical, 1=cell line, 2=mouse model, 3=other."""
    text = _record_text(record)
    if any(_contains_phrase(text, t) for t in _HUMAN_CLINICAL_TERMS):
        return 0
    if any(_contains_phrase(text, t) for t in _CELL_LINE_TERMS):
        return 1
    if any(_contains_phrase(text, t) for t in _MOUSE_MODEL_TERMS):
        return 2
    return 3


def _is_high_impact(record: LiteratureRecord) -> bool:
    return record.journal in _HIGH_IMPACT_JOURNALS


def _rank_records(records: List[LiteratureRecord]) -> List[LiteratureRecord]:
    """Sort by evidence tier, then high-impact journal, then recency (descending PMID)."""
    return sorted(
        records,
        key=lambda r: (
            _publication_evidence_rank(r),
            0 if _is_high_impact(r) else 1,
            -(int(r.pmid) if r.pmid.isdigit() else 0),
        ),
    )


def _evidence_priority_queries(subject: str) -> list[str]:
    """Queries that surface human clinical > cell line > mouse model evidence."""
    clinical = " OR ".join(f'"{t}"' for t in _HUMAN_CLINICAL_TERMS[:6])
    cell = " OR ".join(f'"{t}"' for t in _CELL_LINE_TERMS[:6])
    mouse = " OR ".join(f'"{t}"' for t in _MOUSE_MODEL_TERMS[:4])
    return [
        f'"{subject}" AND ({clinical})',
        f'"{subject}" AND ({cell})',
        f'"{subject}" AND ({mouse})',
    ]


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
    """Fetch abstracts for a list of PMIDs, including journal and publication type metadata."""
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
            journal_el = article.find(".//MedlineTA")
            journal = journal_el.text.strip() if journal_el is not None and journal_el.text else ""
            pub_types = [
                el.text.strip()
                for el in article.findall(".//PublicationType")
                if el.text
            ]
            if pmid and abstract:
                records.append(LiteratureRecord(
                    pmid=pmid, title=title, abstract=abstract,
                    journal=journal, publication_types=pub_types,
                ))
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
    tumor_type: Optional[str] = None,
) -> List[LiteratureRecord]:
    """
    Primary retrieval: runs multiple PubMed queries in parallel to maximise
    coverage before falling through to the agentic Tier 2.

    Query families (run concurrently):
      1. Gene Name MeSH field query (high precision)
      2. Free-text broadening query
      3. Evidence-priority queries (clinical > cell line > mouse model)
      4. Tumor-type-specific query with alias expansion when tumor_type is supplied
      5. Co-query with each fusion partner
    """
    queries = [
        f'"{gene}"[Gene Name] AND cancer[MeSH Terms]',
        f'"{gene}" AND (cancer OR tumor OR oncology OR carcinoma)',
        *_evidence_priority_queries(gene),
    ]
    if tumor_type:
        tt = _tumor_type_query_fragment(tumor_type)
        queries.insert(0, f'"{gene}" AND {tt}')
    for partner in _fusion_partners(gene, fusions or [])[:2]:
        queries.append(f'"{gene}" AND "{partner}"')
        if tumor_type:
            queries.append(f'"{gene}" AND "{partner}" AND {_tumor_type_query_fragment(tumor_type)}')
    queries = list(dict.fromkeys(queries))

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

    records = _rank_records(records)
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

Prioritize queries likely to surface human clinical evidence first (patient cohorts,
clinical trials, survival data), then cell-line functional studies, then mouse models.

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
    tumor_type: Optional[str] = None,
) -> List[LiteratureRecord]:
    """
    Fallback retrieval: Claude decides what queries to run.
    Runs an agentic tool-use loop, accumulating unique records across all searches.
    """
    accumulated: Dict[str, LiteratureRecord] = {r.pmid: r for r in initial_records}

    fusion_context = f"Associated fusions: {', '.join(fusions)}" if fusions else ""
    tumor_note = f"Tumor type: {_tumor_type_prompt_note(tumor_type)}" if tumor_type else ""
    initial_summary = _format_initial_records(initial_records)

    user_message = (
        f"Gene: {gene}\n"
        f"{fusion_context}\n"
        f"{tumor_note}\n\n"
        f"{initial_summary}\n\n"
        f"Please search for additional cancer-relevant literature for {gene}. "
        f"Try different queries including aliases, fusion-specific terms, and the supplied tumor type "
        f"(and its aliases listed above) when relevant. "
        f"Prioritize queries that surface human clinical evidence first, then cell-line studies, "
        f"then mouse models. "
        f"Call done() when satisfied or after exhausting useful approaches."
    )

    messages = [{"role": "user", "content": user_message}]
    tool_calls_made = 0
    client = make_async_sdk_client()

    async with httpx.AsyncClient() as http_client:
        while tool_calls_made < MAX_AGENTIC_TOOL_CALLS:
            response = await client.messages.create(
                model=resolve_sdk_model(settings.synthesis_model, "synthesis"),
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
    return _rank_records(list(accumulated.values()))


async def _tier2_local_retrieve(
    gene: str,
    fusions: List[str],
    initial_records: List[LiteratureRecord],
    local_backend: Optional[str],
    tumor_type: Optional[str] = None,
) -> List[LiteratureRecord]:
    """
    Local fallback retrieval for Claude Code mode.

    Claude Code cannot participate in Anthropic SDK tool-use loops, so ask it for
    concrete PubMed query strings, then execute those queries locally.
    """
    accumulated: Dict[str, LiteratureRecord] = {r.pmid: r for r in initial_records}
    fusion_context = f"Associated fusions: {', '.join(fusions)}" if fusions else "Associated fusions: none"
    tumor_note = f"Tumor type: {_tumor_type_prompt_note(tumor_type)}" if tumor_type else ""
    initial_summary = _format_initial_records(initial_records)
    user_prompt = (
        f"Gene: {gene}\n"
        f"{fusion_context}\n"
        f"{tumor_note}\n\n"
        f"{initial_summary}\n\n"
        "Suggest up to 6 PubMed queries to find direct cancer-relevant evidence for this gene. "
        "Include aliases, fusion partner context, the supplied tumor type (and its aliases listed above), "
        "and pathway or protein-family terms when useful. "
        "Prioritize queries likely to surface human clinical evidence first, then cell-line studies, "
        "then mouse models. Return only queries that should be run against PubMed."
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
    return _rank_records(list(accumulated.values()))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def retrieve_literature(
    gene: str,
    fusions: Optional[List[str]] = None,
    local_mode: bool = False,
    local_backend: Optional[str] = None,
    tumor_type: Optional[str] = None,
) -> tuple:
    """
    Two-tier retrieval with automatic fallthrough.

    Tier 1: direct NCBI query (always runs first, cheap).
    Tier 2: Claude agentic retrieval (only when Tier 1 is insufficient).

    The threshold is settings.min_papers_for_strong_association (default 4).
    Records are ranked by evidence tier (clinical > cell line > mouse > other),
    then high-impact journal, then recency before being passed downstream.

    Returns (records, tier) where tier is 1 or 2.
    """
    try:
        records = await _tier1_retrieve(gene, fusions, tumor_type)
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
        records = await _tier2_local_retrieve(gene, fusions or [], records, local_backend, tumor_type)
    else:
        records = await _tier2_agentic_retrieve(gene, fusions or [], records, tumor_type)
    return records, 2
