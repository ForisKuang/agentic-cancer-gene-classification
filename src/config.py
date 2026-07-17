from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    oncokb_api_token: str = ""
    ncbi_api_key: str = ""
    google_service_account_json: str = ""

    synthesis_model: str = "claude-opus-4-7"
    selection_model: str = "claude-haiku-4-5-20251001"
    pubmed_max_results: int = 50
    min_papers_for_strong_association: int = 4
    max_papers_for_synthesis: int = 8
    max_citations_per_annotation: int = 4
    max_gene_annotation_concurrency: int = 3
    oncokb_gene_cache_ttl_hours: int = 24
    selection_prefilter_limit: int = 24
    selection_chunk_size: int = 10
    selection_chunk_keep: int = 3
    synthesis_evidence_max_chars: int = 900
    evidence_extraction_batch_size: int = 1
    local_tier2_min_prefilter_score: int = 2
    llm_cache_enabled: bool = True
    token_budget_mode: str = "balanced"

    log_level: str = "INFO"


settings = Settings()
