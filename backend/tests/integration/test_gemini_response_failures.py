"""Gemini rejection messages across worker, persistence, logs and HTTP results."""

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from io import BytesIO, StringIO
from types import SimpleNamespace
from uuid import UUID

import pytest
from fakeredis.aioredis import FakeRedis
from google.genai import types
from httpx import ASGITransport, AsyncClient
from pypdf import PdfWriter

from document_processor.api.app import create_app
from document_processor.config import Settings
from document_processor.domain.models import DocumentStatus, NewDocument, PageContent, ParserKind
from document_processor.infrastructure.parsers.gemini_parser import GeminiPdfParser
from document_processor.infrastructure.redis_gateway import RedisSubmissionGateway
from document_processor.infrastructure.redis_repository import RedisDocumentRepository
from document_processor.infrastructure.redis_streams import RedisStreamQueue
from document_processor.services.processing import ProcessingService
from document_processor.worker import DocumentWorker
from document_processor.worker_logging import SafeWorkerFormatter

DOCUMENT_ID = UUID("00000000-0000-4000-8000-000000000021")
PAYLOAD_CANARY = "PROVIDER_PAYLOAD_CANARY"
METADATA_CANARY = "PROVIDER_METADATA_CANARY"
LEASE_CANARY = "LEASE_TOKEN_CANARY"


class StubModels:
    def __init__(self, response: types.GenerateContentResponse) -> None:
        self.response = response
        self.calls = 0

    async def generate_content(self, **_kwargs: object) -> types.GenerateContentResponse:
        self.calls += 1
        return self.response


class NeverSummarizer:
    async def summarize(self, pages: Sequence[PageContent]) -> str:
        raise AssertionError(f"Rejected extraction reached summarization with {len(pages)} pages")


def two_page_pdf() -> bytes:
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "finish_reason", "message"),
    [
        (
            PAYLOAD_CANARY,
            types.FinishReason.MAX_TOKENS,
            "Gemini reached its response limit before a complete extraction could be confirmed. "
            "Try a smaller PDF.",
        ),
        (None, None, "Gemini returned no extraction output."),
        (
            PAYLOAD_CANARY,
            METADATA_CANARY,
            "Gemini returned malformed JSON for the extracted pages.",
        ),
        (
            json.dumps({"pages": [{"index": 0, "markdown": PAYLOAD_CANARY, "extra": True}]}),
            None,
            "Gemini returned an invalid page structure.",
        ),
        (
            json.dumps({"pages": [{"index": 0, "markdown": PAYLOAD_CANARY}]}),
            None,
            "Gemini page count mismatch: expected 2, received 1. The result was rejected.",
        ),
        (
            json.dumps({"pages": [{"index": 0, "markdown": PAYLOAD_CANARY}] * 2}),
            None,
            "Gemini returned invalid page numbering or order. The result was rejected.",
        ),
    ],
)
async def test_rejected_extraction_reaches_failed_result_without_content_leak(
    text: str | None,
    finish_reason: str | None,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = None if text is None else types.Content(parts=[types.Part.from_text(text=text)])
    # Include an unknown enum value without letting test construction normalize it.
    candidate = types.Candidate.model_construct(content=content, finish_reason=finish_reason)
    models = StubModels(types.GenerateContentResponse(candidates=[candidate]))
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    parser = GeminiPdfParser(client=client, model="synthetic-test-model")
    redis = FakeRedis()
    repository = RedisDocumentRepository(redis)
    gateway = RedisSubmissionGateway(redis, ttl_seconds=3600)
    queue = RedisStreamQueue(redis)
    processor = ProcessingService(
        repository=repository,
        parsers={ParserKind.GEMINI: parser},
        summarizer=NeverSummarizer(),
    )
    worker = DocumentWorker(
        queue=queue,
        repository=repository,
        processor=processor,
        consumer_name="diagnostic-test-worker",
        max_attempts=3,
        retry_base_seconds=0,
        retry_jitter_seconds=0,
        reclaim_idle_ms=30_000,
        read_block_ms=1,
        batch_size=1,
        lock_ttl_seconds=30,
        random=lambda: 0,
        token_factory=lambda: LEASE_CANARY,
    )
    log_output = StringIO()
    handler = logging.StreamHandler(log_output)
    handler.setFormatter(SafeWorkerFormatter())
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    caplog.set_level(logging.INFO, logger="document_processor.worker")
    try:
        await gateway.create_batch(
            [
                NewDocument(
                    id=DOCUMENT_ID,
                    filename="synthetic-diagnostic.pdf",
                    parser=ParserKind.GEMINI,
                    status=DocumentStatus.QUEUED,
                    content=two_page_pdf(),
                    created_at=datetime.now(UTC),
                )
            ]
        )
        assert await worker.run_once() == 1
        document = await repository.get(DOCUMENT_ID)
        assert document is not None
        assert document.status is DocumentStatus.FAILED
        assert document.error_code == "invalid_provider_response"
        assert document.error_message == message
        assert document.attempts == 1
        assert document.pages is None
        assert document.summary is None
        assert await repository.get_source(DOCUMENT_ID) is None
        assert (await redis.xpending(queue.stream_name, queue.group_name))["pending"] == 0
        assert models.calls == 1

        app = create_app(
            settings=Settings(), submission_gateway=gateway, document_reader=repository
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get(f"/api/v1/documents/{DOCUMENT_ID}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"] == {"code": "invalid_provider_response", "message": message}
        assert "pages" not in body
        assert "summary" not in body
        records = [json.loads(line) for line in log_output.getvalue().splitlines()]
        failures = [row for row in records if row["event"] == "document_processing_failed"]
        assert len(failures) == 1
        assert failures[0]["document_id"] == str(DOCUMENT_ID)
        assert failures[0]["attempt"] == 1
        for canary in (PAYLOAD_CANARY, METADATA_CANARY, LEASE_CANARY):
            assert canary not in message
            assert canary not in response.text
            assert canary not in log_output.getvalue()
    finally:
        root_logger.removeHandler(handler)
        handler.close()
        await redis.aclose()
