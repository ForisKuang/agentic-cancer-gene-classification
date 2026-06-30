"""
Literature retrieval via NCBI E-utilities (PubMed / PMC).
Searches for each gene in a cancer context and returns ranked abstracts
as grounding context for LLM synthesis. Every generated claim in synthesis
must trace back to a PMID from this retrieval set.
"""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.models.schema import LiteratureRecord

logger = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

# Rate limit: 3 req/s without key, 10/s with key
_RATE_LIMIT_DELAY = 0.34 if not settings.ncbi_api_key else 0.11
_request_semaphore = asyncio.Semaphore(3 if not settings.ncbi_api_key else 10)


def _ncbi_params(extra: dict) -> dict:
    params = {"retmode": "json", **extra}
    if settings.ncbi_api_key:
        params["api_key"] = settings.ncbi_api_key
    return params


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
async def _search_pubmed(gene: str, client: httpx.AsyncClient) -> list[str]:
    """Return a list of PMIDs for cancer-relevant papers about this gene."""
    query = f'"{gene}"[Gene Name] AND cancer[MeSH Terms]'
    params = _ncbi_params(
        {
            "db": "pubmed",
            "term": query,
            "retmax": settings.pubmed_max_results,
            "sort": "relevance",
            "usehistory": "n",
        }
    )

    async with _request_semaphore:
        await asyncio.sleep(_RATE_LIMIT_DELAY)
        resp = await client.get(ESEARCH_URL, params=params, timeout=15.0)
        resp.raise_for_status()

    data = resp.json()
    pmids = data.get("esearchresult", {}).get("idlist", [])

    if not pmids:
        # Broaden search if no results with MeSH term
        params["term"] = f'"{gene}" AND (cancer OR tumor OR oncology OR carcinoma)'
        async with _request_semaphore:
            await asyncio.sleep(_RATE_LIMIT_DELAY)
            resp = await client.get(ESEARCH_URL, params=params, timeout=15.0)
            resp.raise_for_status()
        data = resp.json()
        pmids = data.get("esearchresult", {}).get("idlist", [])

    return pmids


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
async def _fetch_abstracts(pmids: list[str], client: httpx.AsyncClient) -> list[LiteratureRecord]:
    """Fetch full abstracts for a list of PMIDs via eFetch (XML)."""
    if not pmids:
        return []

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "abstract",
        "retmode": "xml",
    }
    if settings.ncbi_api_key:
        params["api_key"] = settings.ncbi_api_key

    async with _request_semaphore:
        await asyncio.sleep(_RATE_LIMIT_DELAY)
        resp = await client.get(EFETCH_URL, params=params, timeout=30.0)
        resp.raise_for_status()

    records: list[LiteratureRecord] = []
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
                records.append(
                    LiteratureRecord(pmid=pmid, title=title, abstract=abstract)
                )
    except ET.ParseError as e:
        logger.warning("XML parse error fetching abstracts: %s", e)

    return records


async def retrieve_literature(gene_symbol: str) -> list[LiteratureRecord]:
    """
    Search PubMed for cancer-relevant literature about a gene and return
    retrieved abstracts. The returned PMIDs form the only allowed citation set
    for LLM synthesis downstream.
    """
    async with httpx.AsyncClient() as client:
        try:
            pmids = await _search_pubmed(gene_symbol, client)
            if not pmids:
                logger.info("No PubMed results for gene: %s", gene_symbol)
                return []
            records = await _fetch_abstracts(pmids, client)
            logger.info(
                "Retrieved %d abstracts for %s (searched %d PMIDs)",
                len(records),
                gene_symbol,
                len(pmids),
            )
            return records
        except httpx.HTTPError as e:
            logger.error("PubMed retrieval failed for %s: %s", gene_symbol, e)
            return []
