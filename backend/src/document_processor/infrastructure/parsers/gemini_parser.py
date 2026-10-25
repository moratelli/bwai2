"""Gemini inline-PDF adapter with structured page validation."""

import asyncio
from collections.abc import Sequence
from typing import Protocol, cast

from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from document_processor.domain.errors import PermanentProcessingError
from document_processor.domain.models import PageContent
from document_processor.infrastructure.parsers.pdf_validation import source_page_count
from document_processor.infrastructure.provider_errors import classify_gemini_error

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

    @property
    def candidates(self) -> Sequence[types.Candidate] | None: ...


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
            raise classify_gemini_error(error, "extraction") from error
        candidates = response.candidates
        if candidates and candidates[0].finish_reason == types.FinishReason.MAX_TOKENS:
            raise PermanentProcessingError(
                "invalid_provider_response",
                "Gemini reached its response limit before a complete extraction could be "
                "confirmed. Try a smaller PDF.",
            )
        return self._validated_pages(response.text, expected_page_count)

    @classmethod
    def _validated_pages(
        cls,
        response_text: str | None,
        expected_page_count: int,
    ) -> tuple[PageContent, ...]:
        if response_text is None or not response_text.strip():
            raise PermanentProcessingError(
                "invalid_provider_response",
                "Gemini returned no extraction output.",
            )
        try:
            payload = GeminiDocumentPayload.model_validate_json(response_text)
        except ValidationError as error:
            invalid_json = any(
                detail["type"] == "json_invalid"
                for detail in error.errors(
                    include_input=False, include_context=False, include_url=False
                )
            )
            message = (
                "Gemini returned malformed JSON for the extracted pages."
                if invalid_json
                else "Gemini returned an invalid page structure."
            )
            raise PermanentProcessingError("invalid_provider_response", message) from error
        if len(payload.pages) != expected_page_count:
            raise PermanentProcessingError(
                "invalid_provider_response",
                f"Gemini page count mismatch: expected {expected_page_count}, "
                f"received {len(payload.pages)}. The result was rejected.",
            )
        if [page.index for page in payload.pages] != list(range(expected_page_count)):
            raise PermanentProcessingError(
                "invalid_provider_response",
                "Gemini returned invalid page numbering or order. The result was rejected.",
            )
        return cls._to_page_content(payload.pages)

    @staticmethod
    def _to_page_content(pages: Sequence[GeminiPagePayload]) -> tuple[PageContent, ...]:
        return tuple(PageContent(index=page.index, markdown=page.markdown) for page in pages)
