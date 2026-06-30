from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


CancerTier = Literal[
    "Class I - Driver",
    "Class II - Likely Driver",
    "Class III - Cancer Relevant",
]

OgTsg = Literal["OG", "TSG", "OG, TSG"]


class ResolvedGene(BaseModel):
    """Result of HGNC normalization for a single gene symbol."""
    input_symbol: str
    canonical_symbol: Optional[str] = None
    hgnc_id: Optional[str] = None
    resolved: bool
    unresolvable: bool = False  # bare Ensembl ID or unannotated locus


class LiteratureRecord(BaseModel):
    pmid: str
    title: str
    abstract: str


class GeneAnnotation(BaseModel):
    """One row in Nicole's spreadsheet, keyed by gene."""

    gene: str
    fusions: List[str] = Field(default_factory=list)
    in_oncokb: Optional[bool] = None  # None when OncoKB token not configured

    cancer_associated: Optional[bool] = None
    cancer_association_rationale: Optional[str] = None
    cancer_associated_gene_tier: Optional[CancerTier] = None
    og_or_tsg: Optional[OgTsg] = None
    cancer_type_prevalence: Optional[str] = None
    gene_class: Optional[str] = None
    signaling_pathways: Optional[str] = None
    gene_summary: Optional[str] = None
    citations: List[str] = Field(default_factory=list)  # verified PMIDs only
    date_annotated: str = Field(
        default_factory=lambda: date.today().strftime("%-m/%-d/%y")
    )

    # Internal quality metadata (not exported to Nicole's sheet)
    retrieval_count: int = 0
    insufficient_evidence: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    error: Optional[str] = None


class AnnotateRequest(BaseModel):
    fusions: List[str] = Field(
        ...,
        description="Gene fusions in GENE1::GENE2 or GENE1--GENE2 format",
        min_length=1,
    )


class AnnotationResult(BaseModel):
    run_id: str
    timestamp: str
    fusions_processed: int
    genes_annotated: int
    annotations: List[GeneAnnotation]
