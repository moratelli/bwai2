"""Mistral OCR adapter with page normalization."""

import asyncio
from base64 import b64encode
from collections.abc import Sequence
from typing import Protocol, cast

from mistralai import Mistral
from mistralai.models import DocumentURLChunk

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.domain.models import PageContent
from document_processor.infrastructure.parsers.pdf_validation import source_page_count
from document_processor.infrastructure.provider_errors import is_retryable_provider_error


class MistralPageResponse(Protocol):
    @property
    def index(self) -> int: ...

    @property
    def markdown(self) -> str: ...


class MistralResponse(Protocol):
    @property
    def pages(self) -> Sequence[MistralPageResponse]: ...


class MistralOcr(Protocol):
    def process(
        self,
        *,
        model: str,
        document: DocumentURLChunk,
    ) -> MistralResponse: ...


class MistralClient(Protocol):
    @property
    def ocr(self) -> MistralOcr: ...


class MistralOcrParser:
    """Extract page Markdown with the synchronous Mistral OCR SDK."""

    def __init__(
        self,
        *,
        model: str,
        client: object | None = None,
        api_key: str | None = None,
    ) -> None:
        if client is None:
            if api_key is None:
                raise ValueError("api_key is required when no Mistral client is supplied")
            client = Mistral(api_key=api_key)
        self._client = cast(MistralClient, client)
        self._model = model

    async def parse(self, content: bytes) -> tuple[PageContent, ...]:
        try:
            response, expected_page_count = await asyncio.to_thread(self._process, content)
        except PermanentProcessingError:
            raise
        except Exception as error:
            raise self._safe_provider_error(error) from error
        return self._validated_pages(response.pages, expected_page_count)

    def _process(self, content: bytes) -> tuple[MistralResponse, int]:
        expected_page_count = source_page_count(content)
        encoded = b64encode(content).decode("ascii")
        document = DocumentURLChunk(
            document_url=f"data:application/pdf;base64,{encoded}",
        )
        response = self._client.ocr.process(model=self._model, document=document)
        return response, expected_page_count

    @staticmethod
    def _validated_pages(
        pages: Sequence[MistralPageResponse],
        expected_page_count: int,
    ) -> tuple[PageContent, ...]:
        indexes = [page.index for page in pages]
        if indexes != list(range(expected_page_count)):
            raise PermanentProcessingError(
                "invalid_provider_response",
                "Mistral returned an invalid page structure.",
            )
        return tuple(PageContent(index=page.index, markdown=page.markdown) for page in pages)

    @staticmethod
    def _safe_provider_error(
        error: Exception,
    ) -> PermanentProcessingError | RetryableProcessingError:
        if is_retryable_provider_error(error, "status_code", "code"):
            return RetryableProcessingError(
                "mistral_temporarily_unavailable",
                "Mistral is temporarily unavailable.",
            )
        return PermanentProcessingError(
            "mistral_rejected_document",
            "Mistral could not process this PDF.",
        )
