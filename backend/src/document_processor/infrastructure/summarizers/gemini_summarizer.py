"""Bounded Gemini Markdown summarization."""

from collections.abc import Sequence
from typing import Protocol, cast

from google import genai
from google.genai import types

from document_processor.domain.errors import PermanentProcessingError
from document_processor.domain.models import PageContent
from document_processor.infrastructure.gemini_requests import request_gemini
from document_processor.ports import ProviderRequestGate


class GeminiSummaryResponse(Protocol):
    @property
    def text(self) -> str | None: ...

    @property
    def candidates(self) -> Sequence[types.Candidate] | None: ...


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
        request_gate: ProviderRequestGate | None = None,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if client is None:
            if api_key is None:
                raise ValueError("api_key is required when no Gemini client is supplied")
            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
            )
        self._client = cast(GeminiSummaryClient, client)
        self._model = model
        self._request_gate = request_gate
        self._chunk_size = chunk_size

    async def summarize(self, pages: Sequence[PageContent]) -> str:
        if not any(page.markdown.strip() for page in pages):
            raise PermanentProcessingError(
                "empty_extraction",
                "No readable text was extracted. "
                "If you used Fast text, it cannot read scanned pages; "
                "try Rich Markdown (Gemini) or a text-based PDF.",
            )

        chunks = self._page_chunks(pages)
        if len(chunks) == 1:
            return await self._request(
                "Summarize this document as concise, accurate Markdown. "
                "Retain its key facts and conclusions.",
                chunks[0],
            )
        partials = [
            await self._request(
                "Summarize this section as concise, accurate Markdown. "
                "Retain facts needed for a later whole-document synthesis.",
                chunk,
            )
            for chunk in chunks
        ]
        blocks = self._section_blocks(partials)
        for reduction in range(4):
            groups = self._pack_blocks(blocks)
            if len(groups) == 1:
                return await self._request(
                    "Synthesize these section summaries into one coherent Markdown summary. "
                    "Remove repetition without dropping key facts.",
                    groups[0],
                )
            if reduction == 3:
                break
            previous_size = self._rendered_size(blocks)
            partials = [
                await self._request(
                    "Compress these section summaries into concise, accurate Markdown. "
                    "Preserve the key facts while reducing their length.",
                    group,
                )
                for group in groups
            ]
            blocks = self._section_blocks(partials)
            if self._rendered_size(blocks) >= previous_size:
                raise self._too_large()
        raise self._too_large()

    def _page_chunks(self, pages: Sequence[PageContent]) -> tuple[str, ...]:
        return self._pack_blocks(
            [f"## Page {page.index + 1}\n\n{page.markdown.strip()}" for page in pages]
        )

    def _pack_blocks(self, blocks: Sequence[str]) -> tuple[str, ...]:
        chunks: list[str] = []
        current = ""
        for block in blocks:
            if len(block) > self._chunk_size:
                if current:
                    chunks.append(current)
                for offset in range(0, len(block), self._chunk_size):
                    piece = block[offset : offset + self._chunk_size]
                    if offset + self._chunk_size < len(block):
                        chunks.append(piece)
                    else:
                        current = piece
            elif current and len(current) + 2 + len(block) > self._chunk_size:
                chunks.append(current)
                current = block
            else:
                current = f"{current}\n\n{block}" if current else block
        if current:
            chunks.append(current)
        return tuple(chunks)

    @staticmethod
    def _section_blocks(partials: Sequence[str]) -> tuple[str, ...]:
        return tuple(
            f"## Section summary {index}\n\n{partial}"
            for index, partial in enumerate(partials, start=1)
        )

    @staticmethod
    def _rendered_size(blocks: Sequence[str]) -> int:
        return sum(len(block) for block in blocks) + 2 * max(0, len(blocks) - 1)

    @staticmethod
    def _too_large() -> PermanentProcessingError:
        return PermanentProcessingError(
            "summary_too_large",
            "The summary could not be reduced within the configured content limit.",
        )

    async def _request(self, instruction: str, content: str) -> str:
        if len(content) > self._chunk_size:
            raise self._too_large()
        prompt = f"{instruction}\n\n{content}"
        response = await request_gemini(
            lambda: self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(temperature=0),
            ),
            stage="summarization",
            gate=self._request_gate,
        )
        candidates = response.candidates
        if candidates and candidates[0].finish_reason == types.FinishReason.MAX_TOKENS:
            raise PermanentProcessingError(
                "invalid_provider_response",
                "Gemini reached its response limit before a complete summary could be confirmed.",
            )
        summary = response.text
        if summary is None or not summary.strip():
            raise PermanentProcessingError(
                "invalid_provider_response",
                "Gemini returned an invalid summary.",
            )
        return summary.strip()
