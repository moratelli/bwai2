"""Processing failures stay isolated across worker, Redis and result boundaries."""

import json
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from io import BytesIO, StringIO
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from google.genai import types
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from reportlab.pdfgen import canvas

from document_processor.api.app import create_app
from document_processor.config import Settings
from document_processor.domain.errors import RetryableProcessingError
from document_processor.domain.models import (
    DocumentStatus,
    NewDocument,
    PageContent,
    ParserKind,
    StateTransition,
)
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
def stream_name() -> str:
    return f"test-edge-pipeline:{uuid4()}"


@pytest.fixture
async def redis(queue_redis: Redis, stream_name: str) -> AsyncIterator[Redis]:
    try:
        yield queue_redis
    finally:
        keys = [stream_name]
        for identifier in (FAILED_ID, VALID_ID):
            keys.extend(
                [
                    RedisDocumentRepository.document_key(identifier),
                    RedisDocumentRepository.source_key(identifier),
                    RedisDocumentRepository.lock_key(identifier),
                ]
            )
        await queue_redis.delete(*keys)


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
    *,
    reclaim_idle_ms: int = 30_000,
) -> DocumentWorker:
    return DocumentWorker(
        queue=queue,
        repository=repository,
        processor=ProcessingService(repository=repository, parsers=parsers, summarizer=summarizer),
        consumer_name="edge-case-worker",
        max_attempts=3,
        retry_base_seconds=0,
        retry_jitter_seconds=0,
        reclaim_idle_ms=reclaim_idle_ms,
        read_block_ms=1,
        batch_size=1,
        lock_ttl_seconds=30,
        random=lambda: 0,
        token_factory=lambda: "LEASE_CANARY",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("parser_kind", list(ParserKind))
async def test_pdf_limit_failure_does_not_interrupt_sibling(
    redis: Redis, malformed_xref_pdf: bytes, parser_kind: ParserKind, stream_name: str
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
    gateway = RedisSubmissionGateway(redis, ttl_seconds=3600, stream_name=stream_name)
    queue = RedisStreamQueue(redis, stream_name=stream_name)
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
    assert await redis.xlen(stream_name) == 1

    assert await worker.run_once() == 1
    completed = await repository.get(VALID_ID)
    assert completed is not None
    assert completed.status is DocumentStatus.COMPLETED
    assert completed.attempts == 1
    assert completed.pages == (PageContent(index=0, markdown="Valid sibling"),)
    assert completed.summary == "Synthetic sibling summary."
    assert await repository.get_source(VALID_ID) is None
    assert (await redis.xpending(queue.stream_name, queue.group_name))["pending"] == 0
    assert await redis.xlen(stream_name) == 0
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
    redis: Redis,
    stream_name: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
    gateway = RedisSubmissionGateway(redis, ttl_seconds=3600, stream_name=stream_name)
    queue = RedisStreamQueue(redis, stream_name=stream_name)
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
        assert await redis.xlen(stream_name) == 0
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


class NeverParser:
    async def parse(self, content: bytes) -> tuple[PageContent, ...]:
        raise AssertionError("Terminal or absent document reached extraction")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [DocumentStatus.COMPLETED, DocumentStatus.FAILED, None])
async def test_redelivered_terminal_or_absent_document_only_cleans_up_queue(
    redis: Redis, stream_name: str, status: DocumentStatus | None
) -> None:
    repository = RedisDocumentRepository(redis)
    queue = RedisStreamQueue(redis, stream_name=stream_name)
    gateway = RedisSubmissionGateway(redis, ttl_seconds=3600, stream_name=stream_name)
    await gateway.create_batch([new_document(FAILED_ID, text_pdf(), ParserKind.PYPDF)])
    await queue.ensure_group()
    messages = await queue.read_new("previous-worker", count=1, block_ms=1)
    key = repository.document_key(FAILED_ID)
    if status is None:
        await redis.delete(key, repository.source_key(FAILED_ID))
    else:
        previous_owner = "previous-owner"
        assert await repository.acquire_lock(FAILED_ID, previous_owner, ttl_seconds=30)
        assert (
            await repository.admit_attempt(
                FAILED_ID, token=previous_owner, max_attempts=3, updated_at=datetime.now(UTC)
            )
            == 1
        )
        if status is DocumentStatus.COMPLETED:
            result = await repository.complete(
                FAILED_ID,
                token=previous_owner,
                pages=[PageContent(0, "Saved page")],
                summary="Saved summary",
                completed_at=datetime.now(UTC),
                attempts=1,
            )
        else:
            result = await repository.fail(
                FAILED_ID,
                token=previous_owner,
                error_code="invalid_pdf",
                error_message="Saved failure",
                completed_at=datetime.now(UTC),
                attempts=1,
            )
        assert result is StateTransition.APPLIED
        assert await repository.release_lock(FAILED_ID, previous_owner)
    before = await repository.get(FAILED_ID)
    ttl = await redis.pttl(key)
    summarizer = StubSummarizer()
    worker = make_worker(
        queue, repository, {ParserKind.PYPDF: NeverParser()}, summarizer, reclaim_idle_ms=0
    )

    assert await worker.run_once() == 1

    assert await repository.get(FAILED_ID) == before
    assert await repository.get_source(FAILED_ID) is None
    assert (await redis.xpending(stream_name, queue.group_name))["pending"] == 0
    assert await redis.xlen(stream_name) == 0
    assert summarizer.calls == []
    assert messages[0].document_id == FAILED_ID
    if status is None:
        assert await redis.pttl(key) == -2
    else:
        assert 0 < await redis.pttl(key) <= ttl


@pytest.mark.asyncio
async def test_exhausted_failure_is_retained_after_queue_cleanup(
    redis: Redis, stream_name: str
) -> None:
    class RetryableSummarizer:
        calls = 0

        async def summarize(self, pages: Sequence[PageContent]) -> str:
            self.calls += 1
            raise RetryableProcessingError("temporary", "Synthetic temporary failure")

    repository = RedisDocumentRepository(redis)
    queue = RedisStreamQueue(redis, stream_name=stream_name)
    gateway = RedisSubmissionGateway(redis, ttl_seconds=3600, stream_name=stream_name)
    await gateway.create_batch([new_document(FAILED_ID, text_pdf(), ParserKind.PYPDF)])
    summarizer = RetryableSummarizer()
    worker = make_worker(queue, repository, {ParserKind.PYPDF: PyPdfParser()}, summarizer)

    assert await worker.run_once() == 1

    failed = await repository.get(FAILED_ID)
    assert failed is not None and failed.status is DocumentStatus.FAILED
    assert failed.error_code == "retry_exhausted"
    assert failed.attempts == summarizer.calls == 3
    assert await repository.get_source(FAILED_ID) is None
    assert await redis.pttl(repository.document_key(FAILED_ID)) > 0
    assert (await redis.xpending(stream_name, queue.group_name))["pending"] == 0
    assert await redis.xlen(stream_name) == 0


@pytest.mark.asyncio
async def test_lost_ownership_preserves_pending_payload_and_source(
    redis: Redis, stream_name: str
) -> None:
    repository = RedisDocumentRepository(redis)

    class OwnershipLosingParser:
        async def parse(self, content: bytes) -> tuple[PageContent, ...]:
            assert await repository.release_lock(FAILED_ID, "LEASE_CANARY")
            assert await repository.acquire_lock(FAILED_ID, "replacement", ttl_seconds=30)
            return await PyPdfParser().parse(content)

    queue = RedisStreamQueue(redis, stream_name=stream_name)
    gateway = RedisSubmissionGateway(redis, ttl_seconds=3600, stream_name=stream_name)
    content = text_pdf()
    await gateway.create_batch([new_document(FAILED_ID, content, ParserKind.PYPDF)])
    worker = make_worker(
        queue, repository, {ParserKind.PYPDF: OwnershipLosingParser()}, StubSummarizer()
    )

    assert await worker.run_once() == 1

    unfinished = await repository.get(FAILED_ID)
    assert unfinished is not None and unfinished.status is DocumentStatus.PROCESSING
    assert unfinished.pages is None and unfinished.summary is None
    assert await repository.get_source(FAILED_ID) == content
    assert (await redis.xpending(stream_name, queue.group_name))["pending"] == 1
    assert await redis.xlen(stream_name) == 1
    recovered = await queue.reclaim_stale("replacement", minimum_idle_ms=0, count=1)
    assert [message.document_id for message in recovered] == [FAILED_ID]
