"""Deterministic summarizer used only by container smoke tests."""

from collections.abc import Sequence

from document_processor.domain.errors import PermanentProcessingError
from document_processor.domain.models import PageContent


class SmokeTestSummarizer:
    """Produce bounded local Markdown without calling an external provider."""

    def __init__(self, *, max_characters: int = 500) -> None:
        if max_characters <= 0:
            raise ValueError("max_characters must be positive")
        self._max_characters = max_characters

    async def summarize(self, pages: Sequence[PageContent]) -> str:
        extracted = " ".join(page.markdown.strip() for page in pages if page.markdown.strip())
        if not extracted:
            raise PermanentProcessingError(
                "empty_extraction",
                "No extractable text was found. Resubmit scanned PDFs with Mistral OCR.",
            )
        if len(extracted) > self._max_characters:
            extracted = f"{extracted[: self._max_characters - 1].rstrip()}…"
        return f"## Smoke-test summary\n\n{extracted}"
