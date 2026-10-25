#!/usr/bin/env python3
"""Run one live Gemini quality job in a runner-owned disposable Compose stack."""

import asyncio
import json
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import TypedDict, cast
from uuid import UUID, uuid4

import httpx
from check_quality_result import QualityCheck, check_result

ROOT = Path(__file__).resolve().parents[1]
_FIXTURE = ROOT / "fixtures" / "quality-report.pdf"
_MODE_PROBE = (
    "import json; from document_processor.config import Settings; s=Settings(); "
    "print(json.dumps({'fake_summarizer':s.test_summarizer_enabled,"
    "'max_attempts':s.worker_max_attempts,'has_key':s.gemini_available}))"
)
_PROCESSING_CODES = {
    "retry_exhausted",
    "gemini_temporarily_unavailable",
    "summary_too_large",
    "invalid_provider_response",
    "gemini_rejected_document",
    "empty_extraction",
    "invalid_pdf",
}
type Command = Callable[[Sequence[str], Mapping[str, str], float], Awaitable[str]]
type Clock = Callable[[], float]
type Sleep = Callable[[float], Awaitable[None]]


class QualityFailure(Exception):
    """A fixed, non-sensitive check failure code."""


class LiveReport(TypedDict):
    passed: bool
    project: str
    error: str | None
    checks: list[QualityCheck]
    elapsed_seconds: float


def _object(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}


def _remaining(deadline: float, clock: Clock, cap: float | None = None) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise QualityFailure("run_deadline")
    return min(remaining, cap) if cap is not None else remaining


async def _docker(args: Sequence[str], env: Mapping[str, str], budget: float) -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise QualityFailure("docker_unavailable")
    process = await asyncio.create_subprocess_exec(
        executable,
        *args[1:],
        cwd=ROOT,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(budget):
            output, _errors = await process.communicate()
    except BaseException:
        if process.returncode is None:
            with suppress(
                ProcessLookupError
            ):  # The owned process may already have exited.
                process.kill()
        with suppress(
            TimeoutError
        ):  # Preserve the original failure after the bounded drain.
            async with asyncio.timeout(min(2.0, budget)):
                await process.communicate()
        raise
    if process.returncode != 0:
        raise QualityFailure("docker_command_failed")
    return output.decode()


async def _wait_ready(
    client: httpx.AsyncClient, deadline: float, clock: Clock, sleep: Sleep
) -> None:
    while True:
        try:
            response = await client.get(
                "/health/ready", timeout=_remaining(deadline, clock, 10)
            )
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await sleep(_remaining(deadline, clock, 1))


async def _result(
    client: httpx.AsyncClient,
    identifier: str,
    deadline: float,
    clock: Clock,
    sleep: Sleep,
) -> Mapping[str, object]:
    while True:
        try:
            response = await client.get(
                f"/api/v1/documents/{identifier}",
                timeout=_remaining(deadline, clock, 10),
            )
        except httpx.HTTPError:
            await sleep(_remaining(deadline, clock, 1))
            continue
        if response.status_code >= 500:
            await sleep(_remaining(deadline, clock, 1))
            continue
        if response.status_code != 200:
            raise QualityFailure("result_unavailable")
        result = _object(response.json())
        status = result.get("status")
        if status == "completed":
            return result
        if status == "failed":
            code = _object(result.get("error")).get("code")
            safe_code = (
                code
                if isinstance(code, str) and code in _PROCESSING_CODES
                else "processing_failed"
            )
            raise QualityFailure(safe_code)
        if status not in {"queued", "processing"}:
            raise QualityFailure("invalid_result_state")
        await sleep(_remaining(deadline, clock, 1))


async def run_quality(
    *,
    command: Command = _docker,
    transport: httpx.AsyncBaseTransport | None = None,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    budget_seconds: float = 300,
    cleanup_reserve: float = 30,
) -> LiveReport:
    if not 0 < cleanup_reserve < budget_seconds:
        raise ValueError("The cleanup reserve must fit inside the run budget.")
    started = clock()
    deadline = started + budget_seconds - cleanup_reserve
    project = f"bwai2-quality-{uuid4().hex}"
    env = dict(os.environ)
    env.update(
        {
            "APP_PORT": "0",
            "TEST_SUMMARIZER_ENABLED": "false",
            "WORKER_MAX_ATTEMPTS": "1",
            "WORKER_CONCURRENCY": "1",
            "DOCUMENT_TTL_SECONDS": "600",
            "SUMMARY_CHUNK_CHARACTERS": "100000",
        }
    )
    compose = [
        "docker",
        "compose",
        "--project-name",
        project,
        "--project-directory",
        str(ROOT),
        "--file",
        str(ROOT / "compose.yaml"),
    ]
    checks: list[QualityCheck] = []
    error: str | None = None
    stage = "stack_started"
    print(
        json.dumps({"check": "quality_stack", "project": project, "status": "started"})
    )
    try:
        async with asyncio.timeout(_remaining(deadline, clock)):
            await command(
                [
                    *compose,
                    "up",
                    "--build",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    str(max(1, int(_remaining(deadline, clock)))),
                ],
                env,
                _remaining(deadline, clock),
            )
            checks.append({"name": stage, "passed": True})
            stage = "real_worker_mode"
            mode = _object(
                json.loads(
                    await command(
                        [*compose, "exec", "-T", "worker", "python", "-c", _MODE_PROBE],
                        env,
                        _remaining(deadline, clock),
                    )
                )
            )
            if not (
                mode.get("fake_summarizer") is False
                and mode.get("has_key") is True
                and type(mode.get("max_attempts")) is int
                and mode["max_attempts"] == 1
            ):
                raise QualityFailure("unsafe_worker_mode")
            checks.append({"name": stage, "passed": True})
            stage = "isolated_binding"
            binding = (
                await command(
                    [*compose, "port", "frontend", "80"],
                    env,
                    _remaining(deadline, clock),
                )
            ).strip()
            match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", binding)
            if match is None or not 0 < int(match[1]) <= 65535:
                raise QualityFailure("unexpected_binding")
            checks.append({"name": stage, "passed": True})
            async with httpx.AsyncClient(
                base_url=f"http://{binding}",
                transport=transport,
                trust_env=False,
            ) as client:
                stage = "api_ready"
                await _wait_ready(client, deadline, clock, sleep)
                checks.append({"name": stage, "passed": True})
                stage = "upload_accepted"
                pdf = await asyncio.to_thread(_FIXTURE.read_bytes)
                response = await client.post(
                    "/api/v1/documents",
                    data={"parser": "gemini"},
                    files={"files": ("quality-report.pdf", pdf, "application/pdf")},
                    timeout=_remaining(deadline, clock, 10),
                )
                if response.status_code != 202:
                    raise QualityFailure("upload_rejected")
                documents = _object(response.json()).get("documents")
                if not isinstance(documents, list) or len(documents) != 1:
                    raise QualityFailure("invalid_submission")
                identifier = _object(cast(list[object], documents)[0]).get("id")
                if not isinstance(identifier, str):
                    raise QualityFailure("invalid_submission")
                identifier = str(UUID(identifier))
                checks.append({"name": stage, "passed": True})
                stage = "document_result"
                result = await _result(client, identifier, deadline, clock, sleep)
                if not (
                    type(result.get("attempts")) is int
                    and result["attempts"] == 1
                    and result.get("parser") == "gemini"
                    and result.get("id") == identifier
                ):
                    raise QualityFailure("unexpected_job_metadata")
                checks.append({"name": stage, "passed": True})
                stage = "factual_quality"
                quality = await asyncio.to_thread(check_result, result)
                checks.extend(quality["checks"])
                if not quality["passed"]:
                    raise QualityFailure("quality_assertion")
                checks.append({"name": stage, "passed": True})
    except asyncio.CancelledError:
        error = "interrupted"
    except TimeoutError:
        error = "run_deadline"
    except QualityFailure as failure:
        error = str(failure)
    except httpx.HTTPError:
        error = "http_unavailable"
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        error = "quality_runner_failed"
    finally:
        if error is not None:
            checks.append({"name": stage, "passed": False})
        cleanup_ok = False
        try:
            drain_reserve = min(2.0, cleanup_reserve / 2)
            cleanup_deadline = min(
                started + budget_seconds - drain_reserve,
                clock() + cleanup_reserve - drain_reserve,
            )
            async with asyncio.timeout(_remaining(cleanup_deadline, clock)):
                await command(
                    [*compose, "down", "--volumes", "--remove-orphans"],
                    env,
                    _remaining(cleanup_deadline, clock),
                )
                selector = f"label=com.docker.compose.project={project}"
                containers = await command(
                    ["docker", "ps", "--all", "--quiet", "--filter", selector],
                    env,
                    _remaining(cleanup_deadline, clock),
                )
                volumes = await command(
                    ["docker", "volume", "ls", "--quiet", "--filter", selector],
                    env,
                    _remaining(cleanup_deadline, clock),
                )
                cleanup_ok = not containers.strip() and not volumes.strip()
        except (
            QualityFailure,
            OSError,
            ValueError,
            RuntimeError,
            asyncio.CancelledError,
        ):
            cleanup_ok = False
        checks.append({"name": "owned_cleanup", "passed": cleanup_ok})
        if not cleanup_ok and error is None:
            error = "cleanup_failed"
    return {
        "passed": error is None and all(check["passed"] for check in checks),
        "project": project,
        "error": error,
        "checks": checks,
        "elapsed_seconds": round(clock() - started, 3),
    }


def main() -> int:
    report = asyncio.run(run_quality())
    print(json.dumps(report))
    return 0 if report["passed"] else 130 if report["error"] == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
