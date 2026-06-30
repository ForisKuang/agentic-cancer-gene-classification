"""
Gene symbol normalization via the HGNC REST API.
Splits fusions into partner genes and resolves each symbol to its
canonical HUGO identifier. Bare Ensembl IDs and unannotated loci
are routed to the insufficient-evidence path.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Set, Tuple

import httpx

from src.models.schema import ResolvedGene

HGNC_FETCH_URL = "https://rest.genenames.org/fetch/symbol/{symbol}"
HGNC_SEARCH_URL = "https://rest.genenames.org/search/symbol/{symbol}"
ENSEMBL_PATTERN = re.compile(r"^ENSG\d+", re.IGNORECASE)

# Separators used in fusion notation
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


def _is_ensembl_id(symbol: str) -> bool:
    return bool(ENSEMBL_PATTERN.match(symbol))


async def resolve_gene(symbol: str, client: httpx.AsyncClient) -> ResolvedGene:
    """
    Resolve a gene symbol to its canonical HGNC entry.
    Bare Ensembl IDs are flagged as unresolvable (insufficient-evidence path).
    """
    if _is_ensembl_id(symbol):
        return ResolvedGene(
            input_symbol=symbol,
            canonical_symbol=None,
            hgnc_id=None,
            resolved=False,
            unresolvable=True,
        )

    url = HGNC_FETCH_URL.format(symbol=symbol)
    headers = {"Accept": "application/json"}

    try:
        resp = await client.get(url, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        docs = data.get("response", {}).get("docs", [])
        if docs:
            doc = docs[0]
            return ResolvedGene(
                input_symbol=symbol,
                canonical_symbol=doc.get("symbol", symbol),
                hgnc_id=doc.get("hgnc_id"),
                resolved=True,
            )
    except httpx.HTTPError:
        pass

    # Try search as fallback (handles minor capitalisation differences)
    try:
        search_url = HGNC_SEARCH_URL.format(symbol=symbol)
        resp = await client.get(search_url, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        docs = data.get("response", {}).get("docs", [])
        if docs:
            doc = docs[0]
            return ResolvedGene(
                input_symbol=symbol,
                canonical_symbol=doc.get("symbol", symbol),
                hgnc_id=doc.get("hgnc_id"),
                resolved=True,
            )
    except httpx.HTTPError:
        pass

    # Could not resolve — treat as unknown locus (insufficient evidence)
    return ResolvedGene(
        input_symbol=symbol,
        canonical_symbol=symbol,  # keep as-is for display
        hgnc_id=None,
        resolved=False,
        unresolvable=True,
    )


async def normalize_fusions(
    fusions: list[str],
) -> Dict[str, Tuple[ResolvedGene, list[str]]]:
    """
    Given a list of fusion strings, return a mapping of
    canonical_symbol -> (ResolvedGene, [fusion_strings involving this gene]).

    Each gene appears once regardless of how many fusions involve it.
    """
    gene_to_fusions: Dict[str, list[str]] = {}
    gene_symbols: Set[str] = set()

    for fusion in fusions:
        g1, g2 = split_fusion(fusion)
        for g in filter(None, [g1, g2]):
            gene_symbols.add(g)
            gene_to_fusions.setdefault(g, []).append(fusion)

    async with httpx.AsyncClient() as client:
        resolved: Dict[str, ResolvedGene] = {}
        for symbol in gene_symbols:
            rg = await resolve_gene(symbol, client)
            resolved[symbol] = rg

    result: Dict[str, Tuple[ResolvedGene, list[str]]] = {}
    for symbol, rg in resolved.items():
        key = rg.canonical_symbol or symbol
        if key not in result:
            result[key] = (rg, gene_to_fusions[symbol])
        else:
            # Merge fusion lists if same canonical symbol maps from multiple inputs
            result[key][1].extend(gene_to_fusions[symbol])

    return result
