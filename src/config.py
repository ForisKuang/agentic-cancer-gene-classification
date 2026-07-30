from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_sdk_provider: str = "anthropic"
    anthropic_api_key: str = ""
    oncokb_api_token: str = ""
    ncbi_api_key: str = ""

    synthesis_model: str = "claude-opus-4-7"
    selection_model: str = "claude-haiku-4-5-20251001"
    bedrock_synthesis_model: str = ""
    bedrock_selection_model: str = ""
    bedrock_aws_access_key_id: str = ""
    bedrock_aws_secret_access_key: str = ""
    bedrock_aws_session_token: str = ""
    bedrock_aws_default_region: str = ""
    bedrock_aws_profile: str = ""
    bedrock_reverse_proxy: str = ""
    aws_region: str = ""
    aws_default_region: str = ""
    aws_profile: str = ""
    agcg_dev_mode: bool = False
    pubmed_max_results: int = 50
    min_papers_for_strong_association: int = 4
    max_papers_for_synthesis: int = 8
    max_citations_per_annotation: int = 4

    log_level: str = "INFO"


settings = Settings()
