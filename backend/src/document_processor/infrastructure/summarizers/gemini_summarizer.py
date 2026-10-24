"""Bounded Gemini Markdown summarization."""

from collections.abc import Sequence
from typing import Protocol, cast

from google import genai
from google.genai import types

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.domain.models import PageContent
from document_processor.infrastructure.provider_errors import is_retryable_provider_error


class GeminiSummaryResponse(Protocol):
    @property
    def text(self) -> str | None: ...


class GeminiSummaryModels(Protocol):
    async def generate_content(
        self,
        *,
        model: str,
        contents: str,
        config: types.GenerateContentConfig,
    ) -> GeminiSummaryResponse: ...


class GeminiSummaryAsyncClient(Protocol):
    @property
    def models(self) -> GeminiSummaryModels: ...


class GeminiSummaryClient(Protocol):
    @property
    def aio(self) -> GeminiSummaryAsyncClient: ...


class GeminiSummarizer:
    """Create one Markdown summary from bounded page groups."""

    def __init__(
        self,
        *,
        model: str,
        chunk_size: int,
        client: object | None = None,
        api_key: str | None = None,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if client is None:
            if api_key is None:
                raise ValueError("api_key is required when no Gemini client is supplied")
            client = genai.Client(api_key=api_key)
        self._client = cast(GeminiSummaryClient, client)
        self._model = model
        self._chunk_size = chunk_size

    async def summarize(self, pages: Sequence[PageContent]) -> str:
        if not any(page.markdown.strip() for page in pages):
            raise PermanentProcessingError(
                "empty_extraction",
                "No extractable text was found. Resubmit scanned PDFs with Mistral OCR.",
            )

        chunks = self._page_chunks(pages)
        if len(chunks) == 1:
            return await self._request(
                "Summarize this document as concise, accurate Markdown. "
                "Retain its key facts and conclusions.\n\n"
                f"{chunks[0]}"
            )

        partials = [
            await self._request(
                "Summarize this section as concise, accurate Markdown. "
                "Retain facts needed for a later whole-document synthesis.\n\n"
                f"{chunk}"
            )
            for chunk in chunks
        ]
        marked_partials = "\n\n".join(
            f"## Section summary {index}\n\n{partial}"
            for index, partial in enumerate(partials, start=1)
        )
        return await self._request(
            "Synthesize these section summaries into one coherent Markdown summary. "
            "Remove repetition without dropping key facts.\n\n"
            f"{marked_partials}"
        )

    def _page_chunks(self, pages: Sequence[PageContent]) -> tuple[str, ...]:
        blocks = [f"## Page {page.index + 1}\n\n{page.markdown.strip()}" for page in pages]
        chunks: list[str] = []
        current: list[str] = []
        current_size = 0
        for block in blocks:
            separator_size = 2 if current else 0
            if current and current_size + separator_size + len(block) > self._chunk_size:
                chunks.append("\n\n".join(current))
                current = []
                current_size = 0
                separator_size = 0
            current.append(block)
            current_size += separator_size + len(block)
        if current:
            chunks.append("\n\n".join(current))
        return tuple(chunks)

    async def _request(self, prompt: str) -> str:
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0),
            )
        except Exception as error:
            raise self._safe_provider_error(error) from error
        summary = response.text
        if summary is None or not summary.strip():
            raise PermanentProcessingError(
                "invalid_provider_response",
                "Gemini returned an invalid summary.",
            )
        return summary.strip()

    @staticmethod
    def _safe_provider_error(
        error: Exception,
    ) -> PermanentProcessingError | RetryableProcessingError:
        if is_retryable_provider_error(error, "code", "status_code"):
            return RetryableProcessingError(
                "gemini_temporarily_unavailable",
                "Gemini is temporarily unavailable.",
            )
        return PermanentProcessingError(
            "gemini_rejected_document",
            "Gemini could not summarize this document.",
        )
