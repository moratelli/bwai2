"""Offline tests of isolated live-quality orchestration; no Docker or key required."""

import asyncio
import importlib
import json
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT_ID = "00000000-0000-4000-8000-000000000001"
type RunQuality = Callable[..., Awaitable[dict[str, object]]]


def complete_result() -> dict[str, object]:
    return {
        "id": DOCUMENT_ID,
        "status": "completed",
        "attempts": 1,
        "parser": "gemini",
        "pages": [
            {
                "index": 0,
                "markdown": (
                    "North revenue USD 120,000\nSouth revenue USD 80,000\nTotal revenue USD 200,000"
                ),
            },
            {"index": 1, "markdown": "Operating costs USD 140,000\nOperating profit USD 60,000"},
        ],
        "summary": "Total revenue USD 200,000. Operating profit USD 60,000.",
    }


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, duration: float) -> None:
        self.now += duration


class FakeStack:
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.commands: list[list[str]] = []
        self.uploads = 0
        self.requests = 0
        self.waiting = asyncio.Event()

    async def command(self, args: Sequence[str], env: Mapping[str, str], budget: float) -> str:
        assert 0 < budget <= 3
        assert env["APP_PORT"] == "0"
        assert env["TEST_SUMMARIZER_ENABLED"] == "false"
        assert env["WORKER_MAX_ATTEMPTS"] == "1"
        self.commands.append(list(args))
        if args[1] != "compose":
            return "leftover" if self.scenario == "leftover" else ""
        assert args[2] == "--project-name" and args[3].startswith("bwai2-quality-")
        assert args[4:8] == ["--project-directory", str(ROOT), "--file", str(ROOT / "compose.yaml")]
        action = args[8]
        if action == "up" and self.scenario == "startup_hang":
            await asyncio.Event().wait()
        if action == "up" and self.scenario == "startup_failure":
            raise RuntimeError("SECRET_COMMAND_CANARY")
        if action == "down" and self.scenario == "cleanup_failure":
            raise RuntimeError("SECRET_COMMAND_CANARY")
        if action == "port":
            return "0.0.0.0:18080" if self.scenario == "foreign_binding" else "127.0.0.1:18080"
        if action == "exec":
            return json.dumps(
                {
                    "fake_summarizer": self.scenario == "fake_mode",
                    "max_attempts": 3 if self.scenario == "multiple_attempts" else 1,
                    "has_key": self.scenario != "missing_key",
                }
            )
        return ""

    async def http(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        assert request.url.host == "127.0.0.1" and request.url.port == 18080
        if request.url.path == "/health/ready":
            if self.scenario == "cancel":
                self.waiting.set()
                await asyncio.Event().wait()
            return httpx.Response(503 if self.scenario == "readiness_timeout" else 200)
        if request.method == "POST":
            self.uploads += 1
            assert b"gemini" in await request.aread()
            assert b"%PDF" in await request.aread()
            if self.scenario == "upload_timeout":
                raise httpx.ReadTimeout("SECRET_HTTP_CANARY")
            return httpx.Response(202, json={"documents": [{"id": DOCUMENT_ID}]})
        if self.scenario == "processing_timeout":
            return httpx.Response(200, json={"status": "processing"})
        if self.scenario == "provider_failure":
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "error": {"code": "retry_exhausted", "message": "SECRET_BODY_CANARY"},
                },
            )
        result = complete_result()
        if self.scenario == "wrong_facts":
            result["summary"] = "SECRET_BODY_CANARY"
        if self.scenario == "wrong_result_id":
            result["id"] = "00000000-0000-4000-8000-000000000002"
        if self.scenario == "wrong_attempt_count":
            result["attempts"] = 2
        return httpx.Response(200, json=result)


@pytest.fixture
def run_quality(monkeypatch: pytest.MonkeyPatch) -> RunQuality:
    monkeypatch.setattr(sys, "path", [str(ROOT / "scripts"), *sys.path])
    monkeypatch.setenv("APP_URL", "https://unrelated.invalid")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "owner-stack")
    return cast(RunQuality, importlib.import_module("live_quality_test").run_quality)


async def exercise(run_quality: RunQuality, stack: FakeStack) -> dict[str, object]:
    clock = Clock()
    return await run_quality(
        command=stack.command,
        transport=httpx.MockTransport(stack.http),
        clock=clock,
        sleep=clock.sleep,
        budget_seconds=3,
        cleanup_reserve=1,
    )


def assert_owned_cleanup(stack: FakeStack) -> None:
    compose = [args for args in stack.commands if args[1] == "compose"]
    projects = {args[3] for args in compose}
    assert len(projects) == 1 and "owner-stack" not in projects
    assert any(args[8:] == ["down", "--volumes", "--remove-orphans"] for args in compose)


@pytest.mark.asyncio
async def test_success_checks_real_mode_facts_and_owned_cleanup(run_quality: RunQuality) -> None:
    stack = FakeStack("success")
    report = await exercise(run_quality, stack)
    assert report["passed"] is True
    assert stack.uploads == 1
    assert_owned_cleanup(stack)
    assert any(args[1] == "ps" for args in stack.commands)
    assert any(args[1] == "volume" for args in stack.commands)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "fake_mode",
        "multiple_attempts",
        "missing_key",
        "foreign_binding",
        "startup_failure",
        "readiness_timeout",
        "upload_timeout",
        "processing_timeout",
        "provider_failure",
        "wrong_facts",
        "wrong_attempt_count",
        "wrong_result_id",
        "cleanup_failure",
        "leftover",
    ],
)
async def test_failures_are_bounded_safe_and_clean_up_owned_resources(
    run_quality: RunQuality,
    scenario: str,
) -> None:
    stack = FakeStack(scenario)
    report = await exercise(run_quality, stack)
    assert report["passed"] is False
    assert "CANARY" not in json.dumps(report)
    assert stack.uploads <= 1
    if scenario in {"fake_mode", "multiple_attempts", "missing_key", "foreign_binding"}:
        assert stack.uploads == 0
    assert_owned_cleanup(stack)


@pytest.mark.asyncio
async def test_a_hung_operation_hits_the_outer_deadline(run_quality: RunQuality) -> None:
    stack = FakeStack("startup_hang")
    async with asyncio.timeout(1):
        report = await run_quality(
            command=stack.command,
            transport=httpx.MockTransport(stack.http),
            budget_seconds=0.1,
            cleanup_reserve=0.05,
        )
    assert report["passed"] is False
    assert report["error"] == "run_deadline"
    assert_owned_cleanup(stack)


@pytest.mark.asyncio
async def test_cancellation_still_cleans_up_the_owned_stack(run_quality: RunQuality) -> None:
    stack = FakeStack("cancel")
    task = asyncio.create_task(exercise(run_quality, stack))
    await stack.waiting.wait()
    task.cancel()
    report = await task
    assert report["passed"] is False
    assert report["error"] == "interrupted"
    assert_owned_cleanup(stack)
