"""Gemini inline-PDF adapter with structured page validation."""

import asyncio
from collections.abc import Sequence
from typing import Protocol, cast

from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.domain.models import PageContent
from document_processor.infrastructure.parsers.pdf_validation import source_page_count
from document_processor.infrastructure.provider_errors import is_retryable_provider_error

_EXTRACTION_PROMPT = """Convert every PDF page to faithful Markdown.
Return one item per source page in source order. Use zero-based, contiguous page indexes.
Preserve headings, lists, tables, and readable text.
Return an empty markdown string for a blank page. Do not summarize or omit pages.
"""

GEMINI_DOCUMENT_SCHEMA: dict[str, object] = {
    "type": "OBJECT",
    "properties": {
        "pages": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "index": {"type": "INTEGER"},
                    "markdown": {"type": "STRING"},
                },
                "required": ["index", "markdown"],
            },
        }
    },
    "required": ["pages"],
}


class GeminiPagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    markdown: str


class GeminiDocumentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: list[GeminiPagePayload] = Field(min_length=1)


class GeminiResponse(Protocol):
    @property
    def text(self) -> str | None: ...


class GeminiModels(Protocol):
    async def generate_content(
        self,
        *,
        model: str,
        contents: list[types.Part],
        config: types.GenerateContentConfig,
    ) -> GeminiResponse: ...


class GeminiAsyncClient(Protocol):
    @property
    def models(self) -> GeminiModels: ...


class GeminiClient(Protocol):
    @property
    def aio(self) -> GeminiAsyncClient: ...


class GeminiPdfParser:
    """Extract ordered Markdown pages using Gemini's inline PDF support."""

    def __init__(
        self,
        *,
        model: str,
        client: object | None = None,
        api_key: str | None = None,
    ) -> None:
        if client is None:
            if api_key is None:
                raise ValueError("api_key is required when no Gemini client is supplied")
            client = genai.Client(api_key=api_key)
        self._client = cast(GeminiClient, client)
        self._model = model

    async def parse(self, content: bytes) -> tuple[PageContent, ...]:
        expected_page_count = await asyncio.to_thread(source_page_count, content)
        contents = [
            types.Part.from_text(text=_EXTRACTION_PROMPT),
            types.Part.from_bytes(data=content, mime_type="application/pdf"),
        ]
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=GEMINI_DOCUMENT_SCHEMA,
            temperature=0,
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )
        except Exception as error:
            raise self._safe_provider_error(error) from error
        return self._validated_pages(response.text, expected_page_count)

    @classmethod
    def _validated_pages(
        cls,
        response_text: str | None,
        expected_page_count: int,
    ) -> tuple[PageContent, ...]:
        try:
            if response_text is None:
                raise ValueError("missing response text")
            payload = GeminiDocumentPayload.model_validate_json(response_text)
            indexes = [page.index for page in payload.pages]
            if indexes != list(range(expected_page_count)):
                raise ValueError("page indexes must match every source page")
        except (ValidationError, ValueError) as error:
            raise PermanentProcessingError(
                "invalid_provider_response",
                "Gemini returned an invalid page structure.",
            ) from error
        return cls._to_page_content(payload.pages)

    @staticmethod
    def _to_page_content(pages: Sequence[GeminiPagePayload]) -> tuple[PageContent, ...]:
        return tuple(PageContent(index=page.index, markdown=page.markdown) for page in pages)

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
            "Gemini could not process this PDF.",
        )
