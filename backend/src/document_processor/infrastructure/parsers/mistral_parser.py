"""Mistral OCR adapter with page normalization."""

import asyncio
from base64 import b64encode
from collections.abc import Sequence
from typing import Protocol, cast

from mistralai import Mistral
from mistralai.models import DocumentURLChunk

from document_processor.domain.errors import PermanentProcessingError, RetryableProcessingError
from document_processor.domain.models import PageContent


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
            response = await asyncio.to_thread(self._process, content)
        except Exception as error:
            raise self._safe_provider_error(error) from error
        return self._validated_pages(response.pages)

    def _process(self, content: bytes) -> MistralResponse:
        encoded = b64encode(content).decode("ascii")
        document = DocumentURLChunk(
            document_url=f"data:application/pdf;base64,{encoded}",
        )
        return self._client.ocr.process(model=self._model, document=document)

    @staticmethod
    def _validated_pages(
        pages: Sequence[MistralPageResponse],
    ) -> tuple[PageContent, ...]:
        indexes = [page.index for page in pages]
        if not indexes or indexes != list(range(len(indexes))):
            raise PermanentProcessingError(
                "invalid_provider_response",
                "Mistral returned an invalid page structure.",
            )
        return tuple(PageContent(index=page.index, markdown=page.markdown) for page in pages)

    @staticmethod
    def _safe_provider_error(
        error: Exception,
    ) -> PermanentProcessingError | RetryableProcessingError:
        attributes = vars(error)
        raw_status: object = attributes.get("status_code", attributes.get("code"))
        status = raw_status if isinstance(raw_status, int) else None
        if status in {408, 409, 429} or (status is not None and status >= 500):
            return RetryableProcessingError(
                "mistral_temporarily_unavailable",
                "Mistral is temporarily unavailable.",
            )
        return PermanentProcessingError(
            "mistral_rejected_document",
            "Mistral could not process this PDF.",
        )
