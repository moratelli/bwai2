"""Reproducible input bundle for the two-page factual-quality check."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "generate_quality_pdf.py"
FIXTURE = ROOT / "fixtures" / "quality-report.pdf"
MANIFEST = ROOT / "fixtures" / "quality-report.expected.json"


@pytest.fixture
def generated_pdf(tmp_path: Path) -> Path:
    destination = tmp_path / "quality.pdf"
    subprocess.run(  # noqa: S603 - trusted interpreter and repository script; no shell
        [sys.executable, str(GENERATOR), str(destination)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=10,
    )
    return destination


def test_generated_bytes_match_a_second_run_and_the_committed_fixture(
    generated_pdf: Path,
    tmp_path: Path,
) -> None:
    second = tmp_path / "second.pdf"
    subprocess.run(  # noqa: S603 - trusted interpreter and repository script; no shell
        [sys.executable, str(GENERATOR), str(second)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=10,
    )
    assert generated_pdf.read_bytes() == second.read_bytes() == FIXTURE.read_bytes()
    assert generated_pdf.stat().st_size > 1000


@pytest.mark.parametrize(
    ("index", "facts", "absent"),
    [
        (
            0,
            ("North revenue", "120,000", "South revenue", "80,000", "Total revenue", "200,000"),
            "Operating profit",
        ),
        (1, ("Operating costs", "140,000", "Operating profit", "60,000"), "North revenue"),
    ],
)
def test_two_ordered_pages_contain_their_source_facts(
    generated_pdf: Path,
    index: int,
    facts: tuple[str, ...],
    absent: str,
) -> None:
    reader = PdfReader(generated_pdf)
    assert len(reader.pages) == 2
    text = reader.pages[index].extract_text()
    assert "Synthetic" in text
    assert "USD" in text
    assert f"Page {index + 1} of 2" in text
    assert all(fact in text for fact in facts)
    for label, amount in zip(facts[::2], facts[1::2], strict=True):
        assert re.search(rf"{re.escape(label)}:?\s+USD\s+{re.escape(amount)}", text)
    assert absent not in text


def test_manifest_records_the_independent_acceptance_facts() -> None:
    assert json.loads(MANIFEST.read_text()) == {
        "schema_version": 1,
        "fixture": "quality-report.pdf",
        "synthetic": True,
        "page_count": 2,
        "pages": [
            {
                "index": 0,
                "facts": [
                    {"label": "North revenue", "usd": 120000},
                    {"label": "South revenue", "usd": 80000},
                    {"label": "Total revenue", "usd": 200000},
                ],
            },
            {
                "index": 1,
                "facts": [
                    {"label": "Operating costs", "usd": 140000},
                    {"label": "Operating profit", "usd": 60000},
                ],
            },
        ],
        "summary": [
            {"label": "Total revenue", "usd": 200000},
            {"label": "Operating profit", "usd": 60000},
        ],
    }
