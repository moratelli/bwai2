"""Local PDF structure validation shared by provider parsers."""

from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from document_processor.domain.errors import PermanentProcessingError


def source_page_count(content: bytes) -> int:
    """Count source pages or raise a safe terminal PDF error."""
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise PermanentProcessingError(
                "encrypted_pdf",
                "Encrypted PDFs are not supported.",
            )
        count = len(reader.pages)
        if count == 0:
            raise ValueError("PDF has no pages")
        return count
    except PermanentProcessingError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError) as error:
        raise PermanentProcessingError(
            "invalid_pdf",
            "The PDF is corrupt or unsupported.",
        ) from error
