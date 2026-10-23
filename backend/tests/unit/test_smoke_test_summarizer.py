import pytest

from document_processor.domain.errors import PermanentProcessingError
from document_processor.domain.models import PageContent
from document_processor.infrastructure.summarizers.smoke_test_summarizer import (
    SmokeTestSummarizer,
)


@pytest.mark.asyncio
async def test_smoke_summarizer_returns_bounded_deterministic_markdown() -> None:
    summarizer = SmokeTestSummarizer(max_characters=24)

    summary = await summarizer.summarize(
        (
            PageContent(index=0, markdown="First page text."),
            PageContent(index=1, markdown="Second page text."),
        )
    )

    assert summary == "## Smoke-test summary\n\nFirst page text. Second…"


@pytest.mark.asyncio
async def test_smoke_summarizer_rejects_empty_extraction() -> None:
    summarizer = SmokeTestSummarizer()

    with pytest.raises(PermanentProcessingError) as error:
        await summarizer.summarize((PageContent(index=0, markdown="  "),))

    assert error.value.code == "empty_extraction"
