from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_sdk_provider: str = "anthropic"
    anthropic_api_key: str = ""
    oncokb_api_token: str = ""
    ncbi_api_key: str = ""
    oncokbdev_private_access_token: str = ""
    github_repo: str = "oncokb/agentic-cancer-gene-classification"

    synthesis_model: str = "claude-opus-4-7"
    synthesis_fast_model: str = "claude-haiku-4-5-20251001"
    synthesis_model_escalation: bool = True
    synthesis_escalation_min_support_score: float = 0.5
    synthesis_escalation_min_citations: int = 1
    synthesis_escalation_tier2: bool = True
    # When the fast pass's evidence-support score already meets this bar, skip
    # escalation entirely — including the tier2/citations checks below — since
    # a second, slower deep-model call adds latency without adding confidence
    # once the evidence is already this strong.
    synthesis_escalation_sufficient_score: float = 0.75
    core_synthesis_max_tokens: int = 640
    core_synthesis_abstract_chars: int = 500
    core_synthesis_max_papers: int = 6
    core_synthesis_escalation_min_support_score: float = 0.0
    core_synthesis_escalation_tier2: bool = False
    # Core mode already escalates on evidence quality far less than full mode
    # (min_support_score 0.0, tier2 escalation off), so default this high
    # enough that the new short-circuit doesn't change core mode's existing
    # too_few_verified_citations behavior.
    core_synthesis_escalation_sufficient_score: float = 1.01
    selection_model: str = "claude-haiku-4-5-20251001"
    feedback_issue_creation_enabled: bool = True
    feedback_rate_limit_per_hour: int = Field(default=10, ge=1)
    feedback_model: str = "claude-haiku-4-5-20251001"
    retrieval_model: str = "claude-haiku-4-5-20251001"

    # Background (fire-and-forget, never blocking annotation) distillation of
    # retrieved abstracts into the permanent pmid_evidence cache — see
    # src/pipeline/pmid_distillation.py and orchestrator.py's _annotate_gene.
    pmid_distillation_enabled: bool = False
    pmid_distillation_model: str = "claude-haiku-4-5-20251001"
    bedrock_synthesis_model: str = ""
    bedrock_synthesis_fast_model: str = ""
    bedrock_selection_model: str = ""
    bedrock_retrieval_model: str = ""
    bedrock_aws_access_key_id: str = ""
    bedrock_aws_secret_access_key: str = ""
    bedrock_aws_session_token: str = ""
    bedrock_aws_default_region: str = ""
    bedrock_aws_profile: str = ""
    bedrock_reverse_proxy: str = ""
    aws_region: str = ""
    aws_default_region: str = ""
    aws_profile: str = ""
    acgc_dev_mode: bool = False
    pubmed_max_results: int = 50
    fusion_evidence_max_results: int = 20
    fusion_evidence_cache_ttl_seconds: int = 604800
    fusion_evidence_concurrency: int = 2
    fusion_partner_evidence_max_results: int = 20
    fusion_partner_evidence_cache_ttl_seconds: int = 604800
    min_papers_for_strong_association: int = 4
    max_papers_for_synthesis: int = 8
    max_citations_per_annotation: int = 8
    annotation_gene_concurrency: int = 3
    llm_concurrency: int = 2
    pubmed_staged_retrieval: bool = True
    selection_llm_threshold: int = 24

    # Composite pre-ranking heuristic, applied to the deduplicated retrieval pool
    # before the Haiku citation selection pass. Four signals combined into a
    # weighted average (weights renormalized per-paper over whichever signals
    # actually apply — e.g. fusion co-occurrence is skipped for standalone genes).
    citation_score_query_tier_weight: float = 0.30
    citation_score_fusion_cooccurrence_weight: float = 0.20
    citation_score_recency_weight: float = 0.15
    citation_score_publication_type_weight: float = 0.35
    # Recency decays as 0.5 ** (age_years / half_life) — larger relaxes the decay
    # for genes whose key literature skews older.
    citation_score_recency_half_life_years: float = 8.0
    # How many extra review-leaning papers (from the context-weighted ranking) to
    # merge into the citation-ranked candidate pool as background-framing support.
    citation_score_review_supplement_count: int = 2

    # Publication-type weight tables. "citation" favors original research, for the
    # candidate pool synthesis draws verified PMID citations from. "context" favors
    # reviews/meta-analyses, for the small supplement used only for summary framing.
    citation_score_pubtype_trial_weight: float = 1.0
    citation_score_pubtype_comparative_weight: float = 0.85
    citation_score_pubtype_original_research_weight: float = 0.8
    citation_score_pubtype_meta_analysis_weight: float = 0.6
    citation_score_pubtype_case_report_weight: float = 0.5
    citation_score_pubtype_review_weight: float = 0.4
    citation_score_pubtype_editorial_weight: float = 0.2

    context_score_pubtype_review_weight: float = 1.0
    context_score_pubtype_meta_analysis_weight: float = 0.9
    context_score_pubtype_trial_weight: float = 0.6
    context_score_pubtype_comparative_weight: float = 0.6
    context_score_pubtype_original_research_weight: float = 0.5
    context_score_pubtype_case_report_weight: float = 0.3
    context_score_pubtype_editorial_weight: float = 0.15
    annotation_job_ttl_seconds: int = 3600

    redis_url: str = "redis://localhost:6379/0"
    redis_cache_ttl_seconds: int = 86400

    # Redis Sentinel (master/replica with automatic failover). When enabled,
    # takes over from redis_url entirely for cache connections.
    redis_sentinel_enabled: bool = False
    redis_sentinel_hosts: str = ""  # comma-separated host:port, e.g. "sentinel-0:26379,sentinel-1:26379"
    redis_sentinel_master_set: str = "mymaster"
    redis_sentinel_password: str = ""

    gene_cache_enabled: bool = True
    gene_cache_oncokb_check_days: int = 90
    gene_cache_high_support_days: int = 60
    gene_cache_medium_support_days: int = 30
    gene_cache_low_support_days: int = 14
    gene_cache_final_annotation_days: int = 180
    gene_cache_high_support_threshold: float = 0.8
    gene_cache_medium_support_threshold: float = 0.5
    gene_cache_freshness_pmids: int = 20

    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "acgc"
    mysql_password: str = ""
    mysql_database: str = "acgc"

    # Alternative to MYSQL_HOST/MYSQL_PORT: a JDBC-style connection string
    # (e.g. "jdbc:mysql://host:3306"), as used by this org's shared RDS
    # secrets. When set, host/port are parsed from it and take precedence
    # over MYSQL_HOST/MYSQL_PORT. DB_USERNAME/DB_PASSWORD similarly take
    # precedence over MYSQL_USER/MYSQL_PASSWORD when set. MYSQL_DATABASE
    # is unaffected — a JDBC URL identifies a server, not a database.
    db_url: str = ""
    db_username: str = ""
    db_password: str = ""

    fusion_annotation_api_enabled: bool = False
    fusion_annotation_api_base_url: str = ""
    fusion_annotation_api_timeout_seconds: float = 15.0
    fusion_context_cache_ttl_seconds: int = 604800

    # Optional: OpenEvidence clinical-guideline / trial evidence lookup.
    # Off by default. When enabled, the UI renders an independent, on-demand
    # card per gene from GET /v1/genes/{gene}/openevidence (see main.py) —
    # never part of core gene annotation or the synthesis prompt, and its
    # citations are never treated as verified PMIDs. Live (uncached) calls
    # require org-provisioned API access (Order Form) — see
    # github.com/oncokb/oe-api-exp; already-cached results are served without
    # a key. Leave disabled until access, pricing, rate limits, and
    # redistribution rights are confirmed.
    openevidence_enabled: bool = False
    openevidence_api_key: str = ""
    openevidence_base_url: str = "https://api.openevidence.com"
    openevidence_model: str = "darwin"
    # A live-verified smoke test against the real API took ~220s and still
    # hadn't finished a single moderately complex clinical question — 60s is
    # a more realistic floor than the old 30s default, but OpenEvidence may
    # still frequently exceed even this for complex questions. That's an
    # accepted tradeoff: this lookup is explicitly best-effort/supplementary
    # (the sidecar endpoint returns {"available": false} on failure), a
    # timeout is not retried (see openevidence.py's
    # _is_transient_openevidence_error), and it never blocks or fails the core
    # gene annotation either way.
    openevidence_timeout_seconds: float = 60.0
    openevidence_cache_ttl_seconds: int = 604800
    # Concurrency for benchmarks/warm_openevidence_cache.py, deliberately
    # independent of ANNOTATION_GENE_CONCURRENCY — an offline warmup pass is
    # not live annotation traffic and can safely fan out wider than the
    # semaphore that gates concurrent per-gene annotation requests.
    openevidence_warmup_concurrency: int = 5
    # Currently unused. It was the cooldown for the pre-sidecar
    # OpenEvidence-freshness-driven re-synthesis, which no longer exists now
    # that OpenEvidence is served only by the on-demand sidecar endpoint.
    # Kept so existing deployments that set
    # OPENEVIDENCE_REFRESH_COOLDOWN_SECONDS still load unchanged.
    openevidence_refresh_cooldown_seconds: int = 900
    # Caps concurrent live OpenEvidence calls across ALL requests to
    # GET /v1/genes/{gene}/openevidence (see main.py). Without this, a single
    # batch-result page can fire one call per rendered gene card the moment
    # it loads — e.g. 20 concurrent 130-185s calls — with nothing left to
    # throttle it, since OpenEvidence is not part of annotation_gene_concurrency's
    # gated critical path (see orchestrator.py's _annotate_gene).
    openevidence_sidecar_concurrency: int = 3

    log_level: str = "INFO"

    datadog_metrics_enabled: bool = False
    datadog_metrics_namespace: str = "acgc"
    # Left blank by default so the DogStatsd client falls through to its own
    # DD_DOGSTATSD_URL/DD_AGENT_HOST/DD_DOGSTATSD_PORT env var detection —
    # which is what actually resolves to the Datadog Agent in this cluster
    # (a Unix domain socket injected by the admission controller, not a
    # UDP host:port). Only set these to override that with a fixed target.
    datadog_statsd_host: str = ""
    datadog_statsd_port: int = 0
    datadog_user_id_header: str = "x-user-id"
    # Fixed, low-cardinality watchlist for per-gene latency breakdowns.
    # Tagging gene.total_duration_ms with the raw gene symbol would make it a
    # high-cardinality custom metric (one tag value per unique gene queried);
    # bucketing to this list plus "other" keeps cardinality bounded while still
    # surfacing latency for the recurrent fusion partners that matter most.
    datadog_gene_latency_watchlist: str = (
        "ALK,ROS1,RET,NTRK1,NTRK2,NTRK3,BRAF,EGFR,MET,FGFR1,FGFR2,FGFR3,"
        "ABL1,KMT2A,ETV6,EWSR1,TMPRSS2,ERG,PAX3,PAX7,FOXO1,DDIT3,NUTM1,BCR"
    )


settings = Settings()
