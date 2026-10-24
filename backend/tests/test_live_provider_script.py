"""Exercise the live-check script with a generated PDF and a fake HTTP boundary."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_CONTENT = "synthetic-provider-content"
FAKE_CURL = r"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args[-1].endswith("/health/ready"):
    print("{}")
elif "--form" in args:
    forms = [args[index + 1] for index, arg in enumerate(args) if arg == "--form"]
    parser = next(form.removeprefix("parser=") for form in forms if form.startswith("parser="))
    upload = next(form.removeprefix("files=@") for form in forms if form.startswith("files=@"))
    assert Path(upload.split(";", 1)[0]).read_bytes().startswith(b"%PDF-")
    with open(os.environ["TEST_CURL_LOG"], "a", encoding="utf-8") as log:
        log.write(parser + "\n")
    accepted = {"documents": [{"id": "00000000-0000-4000-8000-000000000001"}]}
    Path(args[args.index("--output") + 1]).write_text(json.dumps(accepted))
    print("202", end="")
else:
    print(Path(os.environ["TEST_PROVIDER_RESULT"]).read_text(), end="")
"""


def completed_result() -> dict[str, object]:
    return {
        "status": "completed",
        "summary": PRIVATE_CONTENT,
        "pages": [{"index": 0, "markdown": PRIVATE_CONTENT}],
    }


def run_live_check(
    tmp_path: Path, result: dict[str, object]
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    curl = binaries / "curl"
    curl.write_text(f"#!{sys.executable}\n{FAKE_CURL}")
    curl.chmod(0o755)
    result_file = tmp_path / "provider-result.json"
    result_file.write_text(json.dumps(result))
    log = tmp_path / "submissions.txt"

    process = subprocess.run(
        ["/bin/bash", "scripts/live_provider_test.sh"],
        cwd=ROOT,
        env={
            "PATH": f"{binaries}{os.pathsep}{os.defpath}",
            "PYTHON_BIN": sys.executable,
            "APP_URL": "http://fixture.invalid",
            "LIVE_PROVIDER_WORK_DIR": str(tmp_path / "live-check"),
            "TEST_CURL_LOG": str(log),
            "TEST_PROVIDER_RESULT": str(result_file),
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    submissions = log.read_text().splitlines() if log.exists() else []
    return process, submissions


def test_live_check_submits_only_gemini_without_printing_provider_content(tmp_path: Path) -> None:
    process, submissions = run_live_check(tmp_path, completed_result())

    assert process.returncode == 0, process.stderr
    assert submissions == ["gemini"]
    assert "gemini live check completed" in process.stdout
    assert PRIVATE_CONTENT not in process.stdout + process.stderr


@pytest.mark.parametrize(
    "override",
    [
        {"summary": "  "},
        {"pages": []},
        {"pages": [{"index": 1, "markdown": PRIVATE_CONTENT}]},
        {"pages": [{"index": 0, "markdown": "  "}]},
        {
            "status": "failed",
            "error": {"code": "gemini_rejected_document", "message": PRIVATE_CONTENT},
        },
    ],
    ids=["empty-summary", "no-pages", "wrong-order", "empty-pages", "failed-document"],
)
def test_live_check_rejects_invalid_results_without_printing_content(
    tmp_path: Path, override: dict[str, object]
) -> None:
    process, submissions = run_live_check(tmp_path, completed_result() | override)

    assert process.returncode != 0
    assert submissions == ["gemini"]
    assert PRIVATE_CONTENT not in process.stdout + process.stderr
