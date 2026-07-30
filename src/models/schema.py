from __future__ import annotations

from datetime import date
from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


LocalBackend = Literal["claude-code", "codex", "antigravity"]


class ResolvedGene(BaseModel):
    """Result of HGNC normalization for a single gene symbol."""
    input_symbol: str
    canonical_symbol: Optional[str] = None
    hgnc_id: Optional[str] = None
    name: Optional[str] = None
    alias_symbols: List[str] = Field(default_factory=list)
    locus_type: Optional[str] = None
    resolved: bool
    unresolvable: bool = False  # bare Ensembl ID or unannotated locus


class LiteratureRecord(BaseModel):
    pmid: str
    title: str
    abstract: str
    journal: str = ""
    publication_types: list[str] = []


class SupportingQuote(BaseModel):
    pmid: str
    quote: str


class GeneAnnotation(BaseModel):
    """One row in Nicole's spreadsheet, keyed by gene."""

    gene: str
    fusions: List[str] = Field(default_factory=list)
    in_oncokb: Optional[bool] = None  # None when OncoKB token not configured

    cancer_associated: Optional[bool] = None
    cancer_association_rationale: Optional[str] = None
    cancer_type_prevalence: Optional[str] = None
    gene_class: Optional[str] = None
    signaling_pathways: Optional[str] = None
    gene_summary: Optional[str] = None
    citations: List[str] = Field(default_factory=list)  # verified PMIDs only
    supporting_quotes: List[SupportingQuote] = Field(default_factory=list)
    date_annotated: str = Field(
        default_factory=lambda: date.today().strftime("%-m/%-d/%y")
    )

    # Internal quality metadata (not exported to Nicole's sheet)
    retrieval_count: int = 0
    retrieved_pmids: List[str] = Field(default_factory=list)
    insufficient_evidence: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    error: Optional[str] = None


class FusionInput(BaseModel):
    """Structured fusion input supporting optional tumor type and breakpoint context."""
    fusion: str = Field(..., description="Gene fusion in GENE1::GENE2 or GENE1--GENE2 format")
    tumor_type: Optional[str] = Field(default=None, description="Tumor type for literature retrieval")
    five_exon: Optional[int] = Field(default=None, description="5' partner exon number at breakpoint")
    three_exon: Optional[int] = Field(default=None, description="3' partner exon number at breakpoint")
    five_genomic: Optional[str] = Field(default=None, description="5' genomic breakpoint (chr:pos)")
    three_genomic: Optional[str] = Field(default=None, description="3' genomic breakpoint (chr:pos)")
    five_transcript: Optional[str] = Field(default=None, description="5' transcript breakpoint")
    three_transcript: Optional[str] = Field(default=None, description="3' transcript breakpoint")


class AnnotateRequest(BaseModel):
    fusions: List[FusionInput] = Field(
        ...,
        description="Gene fusions with optional tumor type and breakpoint context",
        min_length=1,
    )
    local_backend: Optional[LocalBackend] = Field(
        default=None,
        description=(
            "Optional local agent backend for LLM calls. When unset, the Anthropic SDK path is used."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def coerce_string_fusions(cls, data: Any) -> Any:
        """Accept plain fusion strings alongside structured FusionInput dicts."""
        if isinstance(data, dict) and "fusions" in data:
            coerced = []
            for item in data["fusions"]:
                if isinstance(item, str):
                    coerced.append({"fusion": item})
                else:
                    coerced.append(item)
            data = {**data, "fusions": coerced}
        return data


class AnnotationResult(BaseModel):
    run_id: str
    timestamp: str
    fusions_processed: int
    genes_annotated: int
    annotations: List[GeneAnnotation]
