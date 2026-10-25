"""HTTP result metadata reflects real Redis transitions, not partial output."""

from httpx import ASGITransport, AsyncClient

from document_processor.api.app import create_app
from document_processor.config import Settings
from document_processor.domain.provider import AttemptFailure, ProviderFailure
from tests.api.test_document_results import NoopGateway
from tests.integration.test_retry_state import NOW, OWNER, UNTIL, Store
from tests.integration.test_retry_state import store as store


async def test_retry_metadata_follows_redis_state_through_http(store: Store) -> None:
    app = create_app(
        settings=Settings(test_summarizer_enabled=True),
        submission_gateway=NoopGateway(),
        document_reader=store.repo,
    )
    path = f"/api/v1/documents/{store.document.id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await store.repo.record_retry_failure(
            store.document.id,
            token=OWNER,
            failure=AttemptFailure(1, ProviderFailure("rate_limited", "extraction", 429)),
            retry_not_before=UNTIL,
            updated_at=NOW,
        )
        body = (await client.get(path)).json()
        assert body["attempts"] == 1 and body["status"] == "processing"
        assert body["retry_wait"] == {
            "until": "2026-10-25T18:01:00Z",
            "cause": {
                "provider": "gemini",
                "category": "rate_limited",
                "stage": "extraction",
                "attempt": 1,
                "http_status": 429,
            },
        }
        store.server_now = UNTIL
        assert (
            await store.repo.admit_attempt(
                store.document.id, token=OWNER, max_attempts=3, updated_at=UNTIL
            )
            == 2
        )
        assert "retry_wait" not in (await client.get(path)).json()
        await store.repo.record_retry_failure(
            store.document.id,
            token=OWNER,
            failure=AttemptFailure(2, ProviderFailure("timeout", "summarization")),
            retry_not_before=None,
            updated_at=UNTIL,
        )
        await store.repo.fail(
            store.document.id,
            token=OWNER,
            error_code="retry_exhausted",
            error_message="Gemini timed out during summarization.",
            completed_at=UNTIL,
            attempts=2,
        )
        body = (await client.get(path)).json()
        assert body["status"] == "failed" and "retry_wait" not in body
        assert body["error"]["cause"] == {
            "provider": "gemini",
            "category": "timeout",
            "stage": "summarization",
            "attempt": 2,
        }
        assert "pages" not in body and "summary" not in body
        await store.raw.hset(store.key, mapping={"last_failure": b"\xff"})
        response = await client.get(path)
        assert response.status_code == 200 and "cause" not in response.json()["error"]
