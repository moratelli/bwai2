"""Ten synthetic jobs, two real workers, Redis coordination, and fake SDK responses."""

import asyncio
import json
import logging
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import httpx
import pytest
from google.genai import errors, types
from redis.asyncio import Redis
from redis.asyncio.client import Pipeline

from document_processor.api.app import create_app
from document_processor.config import Settings
from document_processor.domain.models import DocumentStatus, NewDocument, ParserKind
from document_processor.infrastructure.gemini_rate_gate import RedisGeminiRateGate
from document_processor.infrastructure.parsers.gemini_parser import GeminiPdfParser
from document_processor.infrastructure.parsers.pypdf_parser import PyPdfParser
from document_processor.infrastructure.redis_gateway import RedisSubmissionGateway
from document_processor.infrastructure.redis_repository import RedisDocumentRepository
from document_processor.infrastructure.redis_streams import RedisStreamQueue
from document_processor.infrastructure.summarizers.gemini_summarizer import GeminiSummarizer
from document_processor.services.processing import ProcessingService
from document_processor.worker import DocumentWorker
from document_processor.worker_logging import SafeWorkerFormatter
from tests.api.test_document_results import NoopGateway
from tests.unit.test_gemini_parser import PDF_BYTES


@dataclass
class ControlledClock:
    milliseconds: int = 1_800_000_000_000
    waiting: dict[asyncio.Future[None], int] = field(
        default_factory=dict[asyncio.Future[None], int]
    )
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.milliseconds / 1000, UTC)

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiting[future] = self.milliseconds + round(seconds * 1000)
        self.changed.set()
        try:
            await future
        finally:
            self.waiting.pop(future, None)

    def advance(self) -> None:
        self.milliseconds = min(self.waiting.values())
        for future, deadline in list(self.waiting.items()):
            if deadline <= self.milliseconds:
                del self.waiting[future]
                future.set_result(None)

    async def drive(
        self,
        workers: list[asyncio.Task[None]],
        inspect_wait: Callable[[], Awaitable[None]],
    ) -> None:
        # Advance only when every live consumer is blocked on a controlled wait. This
        # prevents virtual time moving between an admission and its instant fake SDK call.
        async with asyncio.timeout(15):
            while active := [worker for worker in workers if not worker.done()]:
                if len(self.waiting) == len(active):
                    await inspect_wait()
                    self.advance()
                    continue
                wake = asyncio.create_task(self.changed.wait())
                try:
                    done, _ = await asyncio.wait(
                        [*active, wake], return_when=asyncio.FIRST_COMPLETED
                    )
                    self.changed.clear()
                    for worker in done:
                        if worker is not wake:
                            await worker
                finally:
                    wake.cancel()
                    await asyncio.gather(wake, return_exceptions=True)
            await asyncio.gather(*workers)


@dataclass
class Request:
    document: int
    kind: str
    time_ms: int


class ScriptedSdk:
    def __init__(
        self,
        clock: ControlledClock,
        documents: list[NewDocument],
        repository: RedisDocumentRepository,
    ) -> None:
        self.clock, self.documents, self.repository = clock, documents, repository
        self.sources = {document.content: index for index, document in enumerate(documents)}
        self.calls: list[Request] = []
        self.counts: Counter[tuple[int, str]] = Counter()
        self.not_before = 0
        self.early_calls: list[tuple[int, int]] = []

    async def generate_content(
        self,
        *,
        model: str,
        contents: str | list[types.Part],
        config: types.GenerateContentConfig,
    ) -> types.GenerateContentResponse:
        assert model == "synthetic-model"
        if isinstance(contents, str):
            match = re.search(r"doc-(\d+)", contents)
            assert match is not None
            index = int(match.group(1))
            kind = "synthesis" if contents.startswith("Synthesize") else "section"
        else:
            inline = contents[1].inline_data
            assert inline is not None and inline.data is not None
            index = self.sources[inline.data]
            kind = "extraction"
            assert config.response_mime_type == "application/json"
        now = self.clock.milliseconds
        if now < self.not_before:
            self.early_calls.append((now, self.not_before))
        self.not_before = now + 5000
        self.calls.append(Request(index, kind, now))
        self.counts[index, kind] += 1
        record = await self.repository.get(self.documents[index].id)
        assert record is not None and record.status is DocumentStatus.PROCESSING
        assert 1 <= record.attempts <= 3
        assert await self.repository.get_source(record.id) is not None
        status = 0
        if index == 0 and kind == "extraction" and self.counts[index, kind] == 1:
            status = 429
        elif index == 1 and kind == "section" and self.counts[index, kind] == 1:
            status = 503
        elif index == 7:
            status = 503
        if status:
            self.not_before = now + 60000
            raise errors.APIError(
                status,
                {"error": {"message": "PRIVATE_PROVIDER_CANARY"}},
                httpx.Response(status, headers={"Retry-After": "60"}),
            )
        text = (
            json.dumps(
                {"pages": [{"index": page, "markdown": f"doc-{index} " * 25} for page in range(2)]}
            )
            if kind == "extraction"
            else f"doc-{index} brief."
        )
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    finish_reason=types.FinishReason.STOP,
                    content=types.Content(parts=[types.Part(text=text)]),
                )
            ],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=321, candidates_token_count=15
            ),
        )


async def test_two_workers_recover_without_early_calls_and_isolate_bad_siblings(
    queue_redis: Redis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = ControlledClock()
    namespace = "test-resilience-" + uuid4().hex
    repository = RedisDocumentRepository(queue_redis)
    queue = RedisStreamQueue(queue_redis, stream_name=namespace, group_name="workers")
    documents = [
        NewDocument(
            id=uuid4(),
            filename=f"synthetic-{index}.pdf",
            status=DocumentStatus.QUEUED,
            parser=ParserKind.PYPDF if index == 9 else ParserKind.GEMINI,
            content=b"%PDF-bad"
            if index == 9
            else PDF_BYTES + f"\n% doc-{index} PRIVATE_DOCUMENT_CANARY\n".encode(),
            created_at=clock.now(),
        )
        for index in range(10)
    ]
    sdk = ScriptedSdk(clock, documents, repository)
    client = SimpleNamespace(aio=SimpleNamespace(models=sdk))
    gates: list[RedisGeminiRateGate] = []
    tasks: list[asyncio.Task[None]] = []
    observed: set[tuple[str, str]] = set()

    async def server_time(self: Pipeline) -> tuple[int, int]:
        return clock.milliseconds // 1000, (clock.milliseconds % 1000) * 1000

    monkeypatch.setattr(Pipeline, "time", server_time)

    def worker(index: int) -> DocumentWorker:
        gate = RedisGeminiRateGate(
            queue_redis,
            scope=namespace,
            model="synthetic-model",
            requests_per_minute=12,
            sleep=clock.sleep,
        )
        gates.append(gate)
        processor = ProcessingService(
            repository=repository,
            parsers={
                ParserKind.PYPDF: PyPdfParser(),
                ParserKind.GEMINI: GeminiPdfParser(
                    client=client, model="synthetic-model", request_gate=gate
                ),
            },
            summarizer=GeminiSummarizer(
                client=client, model="synthetic-model", chunk_size=200, request_gate=gate
            ),
            clock=clock.now,
        )
        return DocumentWorker(
            queue=queue,
            repository=repository,
            processor=processor,
            consumer_name=f"test-worker-{index}",
            max_attempts=3,
            retry_base_seconds=15,
            retry_jitter_seconds=0,
            reclaim_idle_ms=30000,
            read_block_ms=1,
            batch_size=1,
            lock_ttl_seconds=120,
            sleep=clock.sleep,
            clock=clock.now,
            random=lambda: 0,
            token_factory=lambda: f"owner-{index}",
            provider_gate=gate,
        )

    async def consume(instance: DocumentWorker) -> None:
        for _ in range(5):
            assert await instance.run_once() == 1

    app = create_app(
        settings=Settings(test_summarizer_enabled=True),
        submission_gateway=NoopGateway(),
        document_reader=repository,
    )
    try:
        await RedisSubmissionGateway(
            queue_redis, ttl_seconds=900, stream_name=namespace
        ).create_batch(documents)
        initial_ttls = [
            await queue_redis.pttl(repository.document_key(document.id)) for document in documents
        ]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api:

            async def inspect_wait() -> None:
                for document in documents:
                    record = await repository.get(document.id)
                    assert record is not None
                    if (
                        record.status is DocumentStatus.PROCESSING
                        and record.retry_not_before is not None
                    ):
                        assert record.last_failure is not None
                        assert record.last_failure.attempt == record.attempts
                        assert await repository.get_source(document.id) is not None
                        body = (await api.get(f"/api/v1/documents/{document.id}")).json()
                        cause = body["retry_wait"]["cause"]
                        assert cause["attempt"] == record.attempts
                        observed.add((cause["category"], cause["stage"]))
                        owner = await queue_redis.get(repository.lock_key(document.id))
                        assert owner in {b"owner-0", b"owner-1"}

            with caplog.at_level(logging.INFO):
                tasks = [asyncio.create_task(consume(worker(index))) for index in range(2)]
                await clock.drive(tasks, inspect_wait)
            assert sdk.early_calls == []
            assert len(sdk.calls) == 38
            assert Counter(call.kind for call in sdk.calls) == {
                "extraction": 13,
                "section": 17,
                "synthesis": 8,
            }
            assert observed == {
                ("rate_limited", "extraction"),
                ("unavailable", "summarization"),
                ("unavailable", "extraction"),
            }
            for index, document in enumerate(documents):
                response = await api.get(f"/api/v1/documents/{document.id}")
                assert response.status_code == 200
                body = response.json()
                assert body["attempts"] == (3 if index == 7 else 2 if index in {0, 1} else 1)
                assert "retry_wait" not in body
                if index == 7:
                    assert body["error"]["code"] == "retry_exhausted"
                    assert body["error"]["cause"]["http_status"] == 503
                    assert body["error"]["cause"]["attempt"] == 3
                elif index == 9:
                    assert body["error"]["code"] == "invalid_pdf" and "cause" not in body["error"]
                else:
                    assert body["status"] == "completed" and len(body["pages"]) == 2
                    assert body["summary"] == f"doc-{index} brief."
                assert "CANARY" not in response.text
                assert await repository.get_source(document.id) is None
                assert await queue_redis.get(repository.lock_key(document.id)) is None
                assert (
                    0
                    < await queue_redis.pttl(repository.document_key(document.id))
                    <= initial_ttls[index]
                )
            assert await queue_redis.xlen(namespace) == 0
            pending = cast(dict[str, object], await queue_redis.xpending(namespace, "workers"))
            assert pending["pending"] == 0
            assert "CANARY" not in "\n".join(
                SafeWorkerFormatter().format(record) for record in caplog.records
            )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        keys = [namespace, *[gate.key for gate in gates]]
        for document in documents:
            keys.extend(
                [
                    repository.document_key(document.id),
                    repository.source_key(document.id),
                    repository.lock_key(document.id),
                ]
            )
        await queue_redis.delete(*keys)
