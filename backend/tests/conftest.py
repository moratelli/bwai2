"""Synthetic PDF input shared by adapter and processing regressions."""

import pytest


@pytest.fixture
def malformed_xref_pdf() -> bytes:
    """Declare more cross-reference entries than this tiny file can contain."""
    return (
        b"%PDF-1.4\n"
        b"xref\n0 1000\n"
        b"0000000000 65535 f \n"
        b"trailer\n<< /Size 1000 >>\n"
        b"startxref\n9\n%%EOF\n"
    )
