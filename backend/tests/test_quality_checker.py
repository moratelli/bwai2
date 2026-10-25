"""Credential-free positive and negative tests of the standalone quality checker."""

import json
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts" / "check_quality_result.py"


class PageFixture(TypedDict):
    index: int
    markdown: str


class ResultFixture(TypedDict):
    status: str
    pages: list[PageFixture]
    summary: str


def valid_result() -> ResultFixture:
    return {
        "status": "completed",
        "pages": [
            {
                "index": 0,
                "markdown": (
                    "| Measure | Amount |\n|---|---|\n"
                    "| North revenue | USD 120,000 |\n"
                    "| South revenue | USD 80,000 |\n"
                    "| Total revenue | USD 200,000 |"
                ),
            },
            {
                "index": 1,
                "markdown": ("Operating costs: USD 140,000\nOperating profit: USD 60,000"),
            },
        ],
        "summary": "Total revenue was USD 200,000. Operating profit was USD 60,000.",
    }


def run_checker(
    tmp_path: Path,
    result: object,
    *,
    raw: str | None = None,
) -> subprocess.CompletedProcess[str]:
    source = tmp_path / "result.json"
    source.write_text(json.dumps(result) if raw is None else raw)
    return subprocess.run(  # noqa: S603 - fixed interpreter and repository script; no shell
        [sys.executable, str(CHECKER), str(source)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


@pytest.mark.parametrize("style", ["comma", "plain", "k", "decimal", "markdown"])
def test_accepts_supported_numeric_and_markdown_equivalents(tmp_path: Path, style: str) -> None:
    result = valid_result()

    def transform(text: str) -> str:
        for amount in (120000, 80000, 200000, 140000, 60000):
            original = f"{amount:,}"
            replacement = {
                "comma": original,
                "plain": str(amount),
                "k": f"{amount // 1000}k",
                "decimal": f"{amount:,}.00",
                "markdown": f"**${original}**",
            }[style]
            text = text.replace(f"USD {original}", replacement)
        return text.replace(" revenue", " **revenue**") if style == "markdown" else text

    for page in result["pages"]:
        page["markdown"] = transform(page["markdown"])
    result["summary"] = transform(result["summary"])
    process = run_checker(tmp_path, result)

    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report["passed"] is True
    assert len(report["checks"]) == 9
    assert all(check["passed"] for check in report["checks"])


@pytest.mark.parametrize(
    ("case", "failed_check"),
    [
        ("missing_page", "page_structure"),
        ("reversed_indices", "page_structure"),
        ("duplicate_index", "page_structure"),
        ("swapped_pages", "page_0_north_revenue"),
        ("empty_page", "page_structure"),
        ("wrong_page_value", "page_0_north_revenue"),
        ("swapped_labels", "page_0_north_revenue"),
        ("empty_summary", "summary_total_revenue"),
        ("merely_nonempty", "summary_total_revenue"),
        ("unlabelled", "summary_total_revenue"),
        ("missing_profit", "summary_operating_profit"),
        ("wrong_summary_value", "summary_total_revenue"),
        ("wrong_fraction", "summary_total_revenue"),
        ("wrong_currency", "summary_total_revenue"),
        ("wrong_unit", "summary_total_revenue"),
        ("negative", "summary_total_revenue"),
        ("prefix_number", "summary_total_revenue"),
        ("conflicting_value", "summary_total_revenue"),
        ("failed_status", "result_status"),
    ],
)
def test_rejects_missing_or_incorrect_labelled_facts(
    tmp_path: Path,
    case: str,
    failed_check: str,
) -> None:
    result = valid_result()
    match case:
        case "missing_page":
            result["pages"].pop()
        case "reversed_indices":
            result["pages"].reverse()
        case "duplicate_index":
            result["pages"][1]["index"] = 0
        case "swapped_pages":
            first, second = result["pages"]
            first["markdown"], second["markdown"] = second["markdown"], first["markdown"]
        case "empty_page":
            result["pages"][0]["markdown"] = " "
        case "wrong_page_value":
            result["pages"][0]["markdown"] = result["pages"][0]["markdown"].replace(
                "120,000", "130,000"
            )
        case "swapped_labels":
            result["pages"][0]["markdown"] = (
                result["pages"][0]["markdown"]
                .replace("North revenue", "placeholder")
                .replace("South revenue", "North revenue")
                .replace("placeholder", "South revenue")
            )
        case "empty_summary":
            result["summary"] = ""
        case "merely_nonempty":
            result["summary"] = "The report describes financial results."
        case "unlabelled":
            result["summary"] = "USD 200,000 and USD 60,000."
        case "missing_profit":
            result["summary"] = "Total revenue USD 200,000."
        case "wrong_summary_value":
            result["summary"] = "Total revenue USD 140,000. Operating profit USD 60,000."
        case "wrong_fraction":
            result["summary"] = result["summary"].replace("200,000", "200,000.01")
        case "wrong_currency":
            result["summary"] = result["summary"].replace("USD 200,000", "EUR 200,000")
        case "wrong_unit":
            result["summary"] = result["summary"].replace("200,000", "200,000 million")
        case "negative":
            result["summary"] = result["summary"].replace("200,000", "-200,000")
        case "prefix_number":
            result["summary"] = result["summary"].replace("200,000", "200,0000")
        case "conflicting_value":
            result["summary"] += " Total revenue USD 300,000."
        case "failed_status":
            result["status"] = "failed"
        case _:
            raise AssertionError(case)
    result["summary"] += " PRIVATE_CONTENT_CANARY"
    process = run_checker(tmp_path, result)

    assert process.returncode == 1
    report = json.loads(process.stdout)
    assert report["passed"] is False
    assert {"name": failed_check, "passed": False} in report["checks"]
    assert "CANARY" not in process.stdout + process.stderr


def test_invalid_json_does_not_expose_input_or_exception_details(tmp_path: Path) -> None:
    process = run_checker(tmp_path, None, raw="{PRIVATE_CONTENT_CANARY")
    assert process.returncode == 1
    assert json.loads(process.stdout) == {
        "passed": False,
        "checks": [{"name": "quality_input", "passed": False}],
    }
    assert "CANARY" not in process.stdout + process.stderr
    assert "Traceback" not in process.stderr


@pytest.mark.parametrize("result", [None, [], {}, {"status": "completed", "pages": "bad"}])
def test_malformed_result_fails_safely(tmp_path: Path, result: object) -> None:
    process = run_checker(tmp_path, result)
    assert process.returncode == 1
    assert json.loads(process.stdout)["passed"] is False
    assert "Traceback" not in process.stderr
