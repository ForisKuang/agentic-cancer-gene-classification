"""
Gene symbol normalization via the HGNC and Ensembl REST APIs.
Splits fusion inputs into partner genes and resolves each symbol to its
canonical HUGO identifier. Ensembl IDs are batch-resolved when possible;
unannotated loci are routed to the insufficient-evidence path.
"""

from __future__ import annotations

import asyncio
import re
from typing import Dict, Iterable, Optional, Set, Tuple

import httpx

from src.models.schema import ResolvedGene

HGNC_FETCH_URL = "https://rest.genenames.org/fetch/symbol/{symbol}"
HGNC_SEARCH_URL = "https://rest.genenames.org/search/symbol/{symbol}"
ENSEMBL_LOOKUP_URL = "https://rest.ensembl.org/lookup/id"
ENSEMBL_PATTERN = re.compile(r"^ENSG\d+", re.IGNORECASE)
HGNC_ID_PATTERN = re.compile(r"Acc:(HGNC:\d+)")
HGNC_TIMEOUT_SECONDS = 5.0
ENSEMBL_TIMEOUT_SECONDS = 8.0
NORMALIZATION_CONCURRENCY = 6

# Separators used in fusion notation.
FUSION_SEPARATORS = re.compile(r"[:]{2}|--|/")


def split_fusion(fusion: str) -> Tuple[str, Optional[str]]:
    """
    Split 'GENE1::GENE2' (or '--' / '/' delimited) into partner symbols.
    Returns (gene1, gene2). If no separator found, returns (fusion, None).
    """
    parts = FUSION_SEPARATORS.split(fusion.strip(), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return parts[0].strip(), None


def is_fusion_input(value: str) -> bool:
    """Return true when an input contains two non-empty fusion partners."""
    gene1, gene2 = split_fusion(value)
    return bool(gene1 and gene2)


def _is_ensembl_id(symbol: str) -> bool:
    return bool(ENSEMBL_PATTERN.match(symbol))


def _ensembl_lookup_id(symbol: str) -> str:
    """Strip optional stable-ID version suffix before Ensembl lookup."""
    return symbol.split(".", 1)[0]


def _unresolvable_gene(symbol: str) -> ResolvedGene:
    return ResolvedGene(
        input_symbol=symbol,
        canonical_symbol=symbol,
        hgnc_id=None,
        resolved=False,
        unresolvable=True,
    )


def _resolved_from_ensembl(symbol: str, doc: Optional[dict]) -> ResolvedGene:
    if not doc or doc.get("object_type") != "Gene" or not doc.get("display_name"):
        return _unresolvable_gene(symbol)

    description = doc.get("description") or ""
    hgnc_match = HGNC_ID_PATTERN.search(description)
    return ResolvedGene(
        input_symbol=symbol,
        canonical_symbol=doc["display_name"],
        hgnc_id=hgnc_match.group(1) if hgnc_match else None,
        resolved=True,
    )


async def _resolve_ensembl_ids(
    symbols: Iterable[str],
    client: httpx.AsyncClient,
) -> Dict[str, ResolvedGene]:
    """
    Resolve Ensembl gene IDs in one POST call.

    Ensembl can be slow, so batching avoids one network round-trip per ENSG ID
    and keeps a bounded timeout. Unresolved IDs fall back to insufficient evidence.
    """
    symbol_list = list(dict.fromkeys(symbols))
    if not symbol_list:
        return {}

    lookup_to_symbols: Dict[str, list[str]] = {}
    for symbol in symbol_list:
        lookup_to_symbols.setdefault(_ensembl_lookup_id(symbol), []).append(symbol)

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        resp = await client.post(
            ENSEMBL_LOOKUP_URL,
            headers=headers,
            json={"ids": list(lookup_to_symbols)},
            timeout=ENSEMBL_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError:
        return {symbol: _unresolvable_gene(symbol) for symbol in symbol_list}

    resolved: Dict[str, ResolvedGene] = {}
    for lookup_id, original_symbols in lookup_to_symbols.items():
        for symbol in original_symbols:
            resolved[symbol] = _resolved_from_ensembl(symbol, data.get(lookup_id))
    return resolved


async def resolve_gene(symbol: str, client: httpx.AsyncClient) -> ResolvedGene:
    """
    Resolve a gene symbol to its canonical HGNC entry.
    Ensembl IDs are resolved through Ensembl before falling back to insufficient evidence.
    """
    if _is_ensembl_id(symbol):
        return (await _resolve_ensembl_ids([symbol], client))[symbol]

    url = HGNC_FETCH_URL.format(symbol=symbol)
    headers = {"Accept": "application/json"}
    lookup_failed = False

    try:
        resp = await client.get(url, headers=headers, timeout=HGNC_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        docs = data.get("response", {}).get("docs", [])
        if docs:
            doc = docs[0]
            return ResolvedGene(
                input_symbol=symbol,
                canonical_symbol=doc.get("symbol", symbol),
                hgnc_id=doc.get("hgnc_id"),
                name=doc.get("name"),
                alias_symbols=doc.get("alias_symbol", []) or [],
                locus_type=doc.get("locus_type"),
                resolved=True,
            )
    except httpx.HTTPError:
        lookup_failed = True

    # Try search as fallback (handles minor capitalisation differences)
    try:
        search_url = HGNC_SEARCH_URL.format(symbol=symbol)
        resp = await client.get(search_url, headers=headers, timeout=HGNC_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        docs = data.get("response", {}).get("docs", [])
        if docs:
            doc = docs[0]
            return ResolvedGene(
                input_symbol=symbol,
                canonical_symbol=doc.get("symbol", symbol),
                hgnc_id=doc.get("hgnc_id"),
                name=doc.get("name"),
                alias_symbols=doc.get("alias_symbol", []) or [],
                locus_type=doc.get("locus_type"),
                resolved=True,
            )
    except httpx.HTTPError:
        lookup_failed = True

    if lookup_failed:
        return ResolvedGene(
            input_symbol=symbol,
            canonical_symbol=symbol,
            hgnc_id=None,
            resolved=False,
            unresolvable=False,
        )

    # Could not resolve — treat as unknown locus (insufficient evidence)
    return _unresolvable_gene(symbol)


async def _resolve_hgnc_symbols_concurrently(
    symbols: Iterable[str],
    client: httpx.AsyncClient,
) -> Dict[str, ResolvedGene]:
    semaphore = asyncio.Semaphore(NORMALIZATION_CONCURRENCY)

    async def resolve_one(symbol: str) -> tuple[str, ResolvedGene]:
        async with semaphore:
            return symbol, await resolve_gene(symbol, client)

    pairs = await asyncio.gather(*(resolve_one(symbol) for symbol in symbols))
    return dict(pairs)


async def normalize_fusions(
    fusions: list[str],
) -> Dict[str, Tuple[ResolvedGene, list[str]]]:
    """
    Given a list of gene or fusion strings, return a mapping of
    canonical_symbol -> (ResolvedGene, [input strings involving this gene]).

    Each gene appears once regardless of how many inputs involve it.
    """
    gene_to_fusions: Dict[str, list[str]] = {}
    gene_symbols: Set[str] = set()

    for fusion in fusions:
        g1, g2 = split_fusion(fusion)
        for g in filter(None, [g1, g2]):
            gene_symbols.add(g)
            gene_to_fusions.setdefault(g, []).append(fusion)

    ensembl_symbols = sorted(symbol for symbol in gene_symbols if _is_ensembl_id(symbol))
    hgnc_symbols = sorted(symbol for symbol in gene_symbols if not _is_ensembl_id(symbol))

    async with httpx.AsyncClient() as client:
        resolved: Dict[str, ResolvedGene] = {}
        resolved.update(await _resolve_ensembl_ids(ensembl_symbols, client))
        resolved.update(await _resolve_hgnc_symbols_concurrently(hgnc_symbols, client))

    result: Dict[str, Tuple[ResolvedGene, list[str]]] = {}
    for symbol, rg in resolved.items():
        key = rg.canonical_symbol or symbol
        if key not in result:
            result[key] = (rg, gene_to_fusions[symbol])
        else:
            # Merge fusion lists if same canonical symbol maps from multiple inputs
            result[key][1].extend(gene_to_fusions[symbol])

    return result
