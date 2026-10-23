"""Runtime configuration loaded from environment variables."""

from pydantic import Field, SecretStr
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
    mistral_api_key: SecretStr | None = None
    mistral_ocr_model: str = "mistral-ocr-latest"
    document_ttl_seconds: int = Field(default=86_400, gt=0)
    max_files_per_batch: int = Field(default=10, gt=0)
    max_file_size_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    summary_chunk_characters: int = Field(default=100_000, gt=0)

    @property
    def gemini_available(self) -> bool:
        """Return whether summarization can be requested."""
        return self.gemini_api_key is not None

    @property
    def mistral_available(self) -> bool:
        """Return whether the optional OCR mode can be requested."""
        return self.mistral_api_key is not None
