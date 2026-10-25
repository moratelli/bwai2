"""Runtime configuration loaded from environment variables."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings shared by the API and worker."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        env_prefix="",
    )

    redis_url: str = "redis://localhost:6379/0"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_requests_per_minute: int = Field(default=12, ge=1, le=1000)
    gemini_rate_limit_scope: str = Field(default="bwai2", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    document_ttl_seconds: int = Field(default=86_400, gt=0)
    max_files_per_batch: int = Field(default=10, gt=0)
    max_file_size_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    summary_chunk_characters: int = Field(default=100_000, gt=0)
    test_summarizer_enabled: bool = False
    stream_name: str = "documents:processing"
    consumer_group: str = "document-workers"
    worker_concurrency: int = Field(default=2, gt=0)
    worker_max_attempts: int = Field(default=3, gt=0)
    worker_retry_base_seconds: float = Field(default=1.0, ge=0)
    worker_retry_jitter_seconds: float = Field(default=0.25, ge=0)
    worker_reclaim_idle_ms: int = Field(default=30_000, gt=0)
    worker_read_block_ms: int = Field(default=5_000, gt=0)
    worker_batch_size: int = Field(default=1, gt=0)
    worker_lock_ttl_seconds: int = Field(default=300, gt=0)

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def empty_credentials_are_missing(cls, value: object) -> object:
        """Treat Compose's empty optional variables as unconfigured."""
        return None if value == "" else value

    @property
    def gemini_available(self) -> bool:
        """Return whether summarization can be requested."""
        return self.gemini_api_key is not None

    @property
    def summary_available(self) -> bool:
        """Return whether production or explicitly test-only summarization is available."""
        return self.gemini_available or self.test_summarizer_enabled
