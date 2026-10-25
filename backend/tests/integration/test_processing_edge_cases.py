"""Processing failures stay isolated across worker, Redis and result boundaries."""

import json
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from io import BytesIO, StringIO
from types import SimpleNamespace
from uuid import UUID

import pytest
from fakeredis.aioredis import FakeRedis
from google.genai import types
from httpx import ASGITransport, AsyncClient
from reportlab.pdfgen import canvas

from document_processor.api.app import create_app
from document_processor.config import Settings
from document_processor.domain.models import DocumentStatus, NewDocument, PageContent, ParserKind
from document_processor.infrastructure.parsers.gemini_parser import GeminiPdfParser
from document_processor.infrastructure.parsers.pypdf_parser import PyPdfParser
from document_processor.infrastructure.redis_gateway import RedisSubmissionGateway
from document_processor.infrastructure.redis_repository import RedisDocumentRepository
from document_processor.infrastructure.redis_streams import RedisStreamQueue
from document_processor.infrastructure.summarizers.gemini_summarizer import GeminiSummarizer
from document_processor.ports import DocumentSummarizer, ParserRegistry
from document_processor.services.processing import ProcessingService
from document_processor.worker import DocumentWorker
from document_processor.worker_logging import SafeWorkerFormatter

FAILED_ID = UUID("00000000-0000-4000-8000-000000000031")
VALID_ID = UUID("00000000-0000-4000-8000-000000000032")


@pytest.fixture
async def redis() -> AsyncIterator[FakeRedis]:
    client = FakeRedis()
    try:
        yield client
    finally:
        await client.aclose()


class StubModels:
    def __init__(self, response: types.GenerateContentResponse) -> None:
        self.response = response
        self.calls = 0

    async def generate_content(self, **_kwargs: object) -> types.GenerateContentResponse:
        self.calls += 1
        return self.response


class StubSummarizer:
    def __init__(self) -> None:
        self.calls: list[tuple[PageContent, ...]] = []

    async def summarize(self, pages: Sequence[PageContent]) -> str:
        self.calls.append(tuple(pages))
        return "Synthetic sibling summary."


def text_pdf() -> bytes:
    output = BytesIO()
    document = canvas.Canvas(output, invariant=True)
    document.drawString(72, 720, "Valid sibling")
    document.showPage()
    document.save()
    return output.getvalue()


def new_document(document_id: UUID, content: bytes, parser: ParserKind) -> NewDocument:
    return NewDocument(
        id=document_id,
        filename="synthetic.pdf",
        parser=parser,
        status=DocumentStatus.QUEUED,
        content=content,
        created_at=datetime.now(UTC),
    )


def make_worker(
    queue: RedisStreamQueue,
    repository: RedisDocumentRepository,
    parsers: ParserRegistry,
    summarizer: DocumentSummarizer,
) -> DocumentWorker:
    return DocumentWorker(
        queue=queue,
        repository=repository,
        processor=ProcessingService(repository=repository, parsers=parsers, summarizer=summarizer),
        consumer_name="edge-case-worker",
        max_attempts=3,
        retry_base_seconds=0,
        retry_jitter_seconds=0,
        reclaim_idle_ms=30_000,
        read_block_ms=1,
        batch_size=1,
        lock_ttl_seconds=30,
        random=lambda: 0,
        token_factory=lambda: "LEASE_CANARY",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("parser_kind", list(ParserKind))
async def test_pdf_limit_failure_does_not_interrupt_sibling(
    redis: FakeRedis, malformed_xref_pdf: bytes, parser_kind: ParserKind
) -> None:
    models = StubModels(
        types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        parts=[
                            types.Part.from_text(
                                text='{"pages":[{"index":0,"markdown":"Valid sibling"}]}'
                            )
                        ]
                    )
                )
            ]
        )
    )
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    repository = RedisDocumentRepository(redis)
    gateway = RedisSubmissionGateway(redis, ttl_seconds=3600)
    queue = RedisStreamQueue(redis)
    summarizer = StubSummarizer()
    worker = make_worker(
        queue,
        repository,
        {
            ParserKind.PYPDF: PyPdfParser(),
            ParserKind.GEMINI: GeminiPdfParser(client=client, model="test"),
        },
        summarizer,
    )
    await gateway.create_batch(
        [
            new_document(FAILED_ID, malformed_xref_pdf, parser_kind),
            new_document(VALID_ID, text_pdf(), parser_kind),
        ]
    )

    assert await worker.run_once() == 1
    failed = await repository.get(FAILED_ID)
    assert failed is not None
    assert failed.status is DocumentStatus.FAILED
    assert failed.error_code == "invalid_pdf"
    assert failed.error_message == "The PDF is corrupt or unsupported."
    assert failed.attempts == 1
    assert failed.pages is None and failed.summary is None
    assert await repository.get_source(FAILED_ID) is None
    assert (await redis.xpending(queue.stream_name, queue.group_name))["pending"] == 0
    assert models.calls == 0
    assert summarizer.calls == []

    assert await worker.run_once() == 1
    completed = await repository.get(VALID_ID)
    assert completed is not None
    assert completed.status is DocumentStatus.COMPLETED
    assert completed.attempts == 1
    assert completed.pages == (PageContent(index=0, markdown="Valid sibling"),)
    assert completed.summary == "Synthetic sibling summary."
    assert await repository.get_source(VALID_ID) is None
    assert (await redis.xpending(queue.stream_name, queue.group_name))["pending"] == 0
    assert len(summarizer.calls) == 1
    assert models.calls == int(parser_kind is ParserKind.GEMINI)

    app = create_app(settings=Settings(), submission_gateway=gateway, document_reader=repository)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get(f"/api/v1/documents/{FAILED_ID}")
        sibling = await http.get(f"/api/v1/documents/{VALID_ID}")
    assert response.status_code == sibling.status_code == 200
    assert response.json()["error"] == {
        "code": "invalid_pdf",
        "message": "The PDF is corrupt or unsupported.",
    }
    assert "pages" not in response.json() and "summary" not in response.json()
    assert sibling.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_truncated_summary_is_persisted_before_ack_without_leaking_content(
    redis: FakeRedis, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    payload = "SUMMARY_PAYLOAD_CANARY"
    metadata = "SUMMARY_METADATA_CANARY"
    candidate = types.Candidate(
        content=types.Content(parts=[types.Part.from_text(text=payload)]),
        finish_reason=types.FinishReason.MAX_TOKENS,
        finish_message=metadata,
    )
    models = StubModels(types.GenerateContentResponse(candidates=[candidate]))
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    summarizer = GeminiSummarizer(client=client, model="test", chunk_size=1000)
    repository = RedisDocumentRepository(redis)
    gateway = RedisSubmissionGateway(redis, ttl_seconds=3600)
    queue = RedisStreamQueue(redis)
    worker = make_worker(queue, repository, {ParserKind.PYPDF: PyPdfParser()}, summarizer)
    acknowledge = queue.acknowledge
    acknowledgements: list[str] = []

    async def observe_acknowledgement(message_id: str) -> int:
        stored = await repository.get(FAILED_ID)
        assert stored is not None and stored.status is DocumentStatus.FAILED
        assert stored.error_code == "invalid_provider_response"
        assert await repository.get_source(FAILED_ID) is None
        acknowledgements.append(message_id)
        return await acknowledge(message_id)

    monkeypatch.setattr(queue, "acknowledge", observe_acknowledgement)
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(SafeWorkerFormatter())
    logger = logging.getLogger()
    logger.addHandler(handler)
    caplog.set_level(logging.INFO, logger="document_processor.worker")
    try:
        await gateway.create_batch([new_document(FAILED_ID, text_pdf(), ParserKind.PYPDF)])
        assert await worker.run_once() == 1
        failed = await repository.get(FAILED_ID)
        assert failed is not None and failed.status is DocumentStatus.FAILED
        assert failed.error_code == "invalid_provider_response"
        message = "Gemini reached its response limit before a complete summary could be confirmed."
        assert failed.error_message == message
        assert failed.attempts == 1
        assert failed.pages is None and failed.summary is None
        assert models.calls == 1
        assert len(acknowledgements) == 1
        assert (await redis.xpending(queue.stream_name, queue.group_name))["pending"] == 0

        app = create_app(
            settings=Settings(), submission_gateway=gateway, document_reader=repository
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get(f"/api/v1/documents/{FAILED_ID}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"] == {"code": "invalid_provider_response", "message": message}
        assert "pages" not in body and "summary" not in body
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        failures = [row for row in records if row["event"] == "document_processing_failed"]
        assert len(failures) == 1
        assert failures[0]["document_id"] == str(FAILED_ID)
        assert failures[0]["attempt"] == 1
        for canary in (payload, metadata, "LEASE_CANARY"):
            assert canary not in response.text
            assert canary not in output.getvalue()
    finally:
        logger.removeHandler(handler)
        handler.close()
