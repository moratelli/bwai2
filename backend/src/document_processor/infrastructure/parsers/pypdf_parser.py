"""Local page-preserving PDF text extraction."""

import asyncio
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError

from document_processor.domain.errors import PermanentProcessingError
from document_processor.domain.models import PageContent


class PyPdfParser:
    """Extract embedded page text without blocking the event loop."""

    async def parse(self, content: bytes) -> tuple[PageContent, ...]:
        return await asyncio.to_thread(self._parse_sync, content)

    @staticmethod
    def _parse_sync(content: bytes) -> tuple[PageContent, ...]:
        try:
            reader = PdfReader(BytesIO(content), strict=False)
            if reader.is_encrypted:
                raise PermanentProcessingError(
                    "encrypted_pdf",
                    "Encrypted PDFs are not supported.",
                )
            return tuple(
                PageContent(index=index, markdown=(page.extract_text() or "").strip())
                for index, page in enumerate(reader.pages)
            )
        except PermanentProcessingError:
            raise
        except (PdfReadError, FileNotDecryptedError, ValueError, KeyError, TypeError) as error:
            raise PermanentProcessingError(
                "invalid_pdf",
                "The PDF is corrupt or unsupported.",
            ) from error
