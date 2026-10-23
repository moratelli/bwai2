"""Behavior tests for local PyPDF extraction."""

from io import BytesIO

import pytest
from pypdf import PdfWriter
from reportlab.pdfgen import canvas

from document_processor.domain.errors import PermanentProcessingError
from document_processor.infrastructure.parsers.pypdf_parser import PyPdfParser


def text_pdf(*pages: str) -> bytes:
    output = BytesIO()
    document = canvas.Canvas(output)
    for text in pages:
        document.drawString(72, 720, text)
        document.showPage()
    document.save()
    return output.getvalue()


def blank_pdf() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def encrypted_pdf() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    writer.write(output)
    return output.getvalue()


@pytest.mark.asyncio
async def test_extracts_text_from_every_page_in_source_order() -> None:
    pages = await PyPdfParser().parse(text_pdf("First page", "Second page"))

    assert len(pages) == 2
    assert [page.index for page in pages] == [0, 1]
    assert "First page" in pages[0].markdown
    assert "Second page" in pages[1].markdown


@pytest.mark.asyncio
async def test_preserves_blank_pages() -> None:
    pages = await PyPdfParser().parse(blank_pdf())

    assert len(pages) == 1
    assert pages[0].index == 0
    assert pages[0].markdown == ""


@pytest.mark.asyncio
async def test_rejects_corrupt_pdf_with_safe_permanent_error() -> None:
    with pytest.raises(PermanentProcessingError) as caught:
        await PyPdfParser().parse(b"%PDF-not-a-real-document")

    assert caught.value.code == "invalid_pdf"
    assert str(caught.value) == "The PDF is corrupt or unsupported."


@pytest.mark.asyncio
async def test_rejects_encrypted_pdf_with_safe_permanent_error() -> None:
    with pytest.raises(PermanentProcessingError) as caught:
        await PyPdfParser().parse(encrypted_pdf())

    assert caught.value.code == "encrypted_pdf"
    assert str(caught.value) == "Encrypted PDFs are not supported."


@pytest.mark.asyncio
async def test_runs_blocking_pypdf_work_in_a_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, tuple[object, ...]]] = []

    async def immediate_to_thread(function: object, *args: object) -> object:
        calls.append((function, args))
        assert callable(function)
        return function(*args)

    monkeypatch.setattr(
        "document_processor.infrastructure.parsers.pypdf_parser.asyncio.to_thread",
        immediate_to_thread,
    )

    pages = await PyPdfParser().parse(blank_pdf())

    assert len(calls) == 1
    assert pages[0].index == 0
