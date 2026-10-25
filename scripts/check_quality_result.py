#!/usr/bin/env python3
"""Check labelled facts in an exported result; this does not establish provider provenance."""

import argparse
import json
import re
import sys
import unicodedata
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import TypedDict, cast

_MANIFEST = (
    Path(__file__).resolve().parents[1] / "fixtures" / "quality-report.expected.json"
)
_AMOUNT = (
    r"(?P<amount>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*k?)"
    r"(?!\w|[.,]\d|\s*(?:million|billion|thousand|eur|gbp|cad|aud|jpy)\b)"
)


class ExpectedFact(TypedDict):
    label: str
    usd: int


class ExpectedPage(TypedDict):
    index: int
    facts: list[ExpectedFact]


class ExpectedManifest(TypedDict):
    schema_version: int
    page_count: int
    pages: list[ExpectedPage]
    summary: list[ExpectedFact]


class QualityCheck(TypedDict):
    name: str
    passed: bool


class QualityReport(TypedDict):
    passed: bool
    checks: list[QualityCheck]


def _record(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}


def _has_fact(text: object, fact: ExpectedFact) -> bool:
    if not isinstance(text, str):
        return False
    normalized = re.sub(r"[*_`#|]", " ", unicodedata.normalize("NFKC", text))
    label = r"\s+".join(re.escape(word) for word in fact["label"].split())
    pattern = re.compile(
        rf"(?<!\w){label}\b\s*(?::|=)?\s*"
        r"(?:(?:was|is|of|amounts to)\s+)?(?:USD\s*|\$\s*)?" + _AMOUNT,
        re.IGNORECASE,
    )
    found = False
    for match in pattern.finditer(normalized):
        number = match["amount"].replace(",", "").strip().lower()
        abbreviated = number.endswith("k")
        actual = Decimal(number[:-1].strip() if abbreviated else number)
        expected = Decimal(fact["usd"]) / 1000 if abbreviated else Decimal(fact["usd"])
        if actual != expected:
            return False
        found = True
    return found


def check_result(value: object) -> QualityReport:
    """Return safe check names and booleans, never result text or metadata."""
    manifest = cast(ExpectedManifest, json.loads(_MANIFEST.read_text()))
    if manifest["schema_version"] != 1:
        raise ValueError("Unsupported quality manifest.")
    result = _record(value)
    raw_pages = result.get("pages")
    pages = (
        [_record(page) for page in cast(list[object], raw_pages)]
        if isinstance(raw_pages, list)
        else []
    )
    expected_pages = manifest["pages"]
    structure_valid = len(pages) == manifest["page_count"] and all(
        type(page.get("index")) is int
        and page["index"] == index
        and isinstance(page.get("markdown"), str)
        and bool(cast(str, page["markdown"]).strip())
        for index, page in enumerate(pages)
    )
    checks: list[QualityCheck] = [
        {"name": "result_status", "passed": result.get("status") == "completed"},
        {"name": "page_structure", "passed": structure_valid},
    ]
    for expected_page in expected_pages:
        index = expected_page["index"]
        text = pages[index].get("markdown") if index < len(pages) else None
        for fact in expected_page["facts"]:
            checks.append(
                {
                    "name": f"page_{index}_{fact['label'].lower().replace(' ', '_')}",
                    "passed": structure_valid and _has_fact(text, fact),
                }
            )
    for fact in manifest["summary"]:
        checks.append(
            {
                "name": f"summary_{fact['label'].lower().replace(' ', '_')}",
                "passed": _has_fact(result.get("summary"), fact),
            }
        )
    return {"passed": all(check["passed"] for check in checks), "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", help="JSON result file, or - for standard input")
    source = cast(str, parser.parse_args().result)
    try:
        value: object = json.loads(
            sys.stdin.read() if source == "-" else Path(source).read_text()
        )
        report = check_result(value)
    except (OSError, ValueError, KeyError, TypeError):
        report = {
            "passed": False,
            "checks": [{"name": "quality_input", "passed": False}],
        }
    print(json.dumps(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
