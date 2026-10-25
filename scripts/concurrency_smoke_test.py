#!/usr/bin/env python3
"""Check concurrent HTTP handling against a disposable, credential-free stack."""

import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.client import HTTPConnection, HTTPResponse
from threading import Barrier, Event
from typing import cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from generate_sample_pdf import build_pdf

UPLOAD_COUNT = 10
HTTP_TIMEOUT = 5
COMPLETION_TIMEOUT = 60
BOUNDARY = "concurrent-smoke-boundary"


def json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object")
    return cast(dict[str, object], value)


def response_json(response: HTTPResponse) -> tuple[int, dict[str, object]]:
    value: object = json.loads(response.read())
    return response.status, json_object(value)


def marker(index: int) -> str:
    return f"Concurrent document {index:02d}"


def upload_body(index: int) -> bytes:
    content = (
        b"%PDF-1.4\nbroken\n" if index == UPLOAD_COUNT - 1 else build_pdf(marker(index))
    )
    prefix = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="parser"\r\n\r\n'
        "pypdf\r\n"
        f"--{BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="concurrent-{index:02d}.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("ascii")
    return prefix + content + f"\r\n--{BOUNDARY}--\r\n".encode("ascii")


class ConcurrentHttpSmoke(unittest.TestCase):
    def setUp(self) -> None:
        address = urlsplit(os.environ.get("APP_URL", "http://127.0.0.1:8080"))
        self.assertEqual(address.scheme, "http")
        self.assertIn(address.hostname, {"127.0.0.1", "localhost", "::1"})
        self.assertIn(address.path, {"", "/"})
        self.host = cast(str, address.hostname)
        self.port = address.port or 80
        self.assert_health()

    def connection(self) -> HTTPConnection:
        return HTTPConnection(self.host, self.port, timeout=HTTP_TIMEOUT)

    def get(self, path: str) -> tuple[int, dict[str, object]]:
        with closing(self.connection()) as connection:
            connection.request("GET", path)
            return response_json(connection.getresponse())

    def assert_health(self) -> None:
        status, body = self.get("/health/live")
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "ok")
        status, body = self.get("/health/ready")
        self.assertEqual(status, 200)
        checks = json_object(body.get("checks"))
        self.assertEqual(checks.get("redis"), "available")
        self.assertEqual(
            checks.get("gemini"),
            "test-only",
            "Use an empty GEMINI_API_KEY and TEST_SUMMARIZER_ENABLED=true",
        )

    def upload(
        self, index: int, started: Barrier, release: Event
    ) -> tuple[int, dict[str, object]]:
        body = upload_body(index)
        with closing(self.connection()) as connection:
            connection.putrequest("POST", "/api/v1/documents")
            connection.putheader(
                "Content-Type", f"multipart/form-data; boundary={BOUNDARY}"
            )
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders()
            # Keep every upload in flight until the main thread probes the API.
            connection.send(body[:-1])
            started.wait(timeout=15)
            if not release.wait(timeout=30):
                raise TimeoutError("Upload release barrier timed out")
            connection.send(body[-1:])
            return response_json(connection.getresponse())

    def test_ten_uploads_do_not_block_reads_or_mix_results(self) -> None:
        started = Barrier(UPLOAD_COUNT + 1)
        release = Event()
        documents: dict[str, int] = {}
        with ThreadPoolExecutor(max_workers=UPLOAD_COUNT) as executor:
            futures = [
                executor.submit(self.upload, index, started, release)
                for index in range(UPLOAD_COUNT)
            ]
            try:
                started.wait(timeout=15)
                self.assert_health()
                status, missing = self.get(f"/api/v1/documents/{uuid4()}")
                self.assertEqual(status, 404)
                self.assertEqual(
                    json_object(missing.get("error")).get("code"), "document_not_found"
                )
            finally:
                release.set()

            for index, future in enumerate(futures):
                status, accepted = future.result(timeout=HTTP_TIMEOUT + 1)
                self.assertEqual(status, 202)
                batch = accepted.get("documents")
                if not isinstance(batch, list):
                    self.fail("Expected an accepted document list")
                self.assertEqual(len(batch), 1)
                document = json_object(batch[0])
                document_id = document.get("id")
                if not isinstance(document_id, str):
                    self.fail("Expected a document identifier")
                self.assertEqual(str(UUID(document_id)), document_id)
                self.assertNotIn(document_id, documents)
                self.assertEqual(document.get("status"), "queued")
                self.assertEqual(document.get("parser"), "pypdf")
                self.assertEqual(
                    document.get("filename"), f"concurrent-{index:02d}.pdf"
                )
                documents[document_id] = index

        pending = dict(documents)
        deadline = time.monotonic() + COMPLETION_TIMEOUT
        while pending:
            self.assert_health()
            for document_id, index in list(pending.items()):
                self.assertLess(
                    time.monotonic(), deadline, "Jobs did not finish in time"
                )
                status, document = self.get(f"/api/v1/documents/{document_id}")
                self.assertEqual(status, 200)
                self.assertEqual(document.get("id"), document_id)
                self.assertEqual(document.get("parser"), "pypdf")
                self.assertEqual(
                    document.get("filename"), f"concurrent-{index:02d}.pdf"
                )
                state = document.get("status")
                if state in ("queued", "processing"):
                    continue
                if index == UPLOAD_COUNT - 1:
                    self.assertEqual(state, "failed")
                    self.assertEqual(
                        json_object(document.get("error")).get("code"), "invalid_pdf"
                    )
                else:
                    self.assertEqual(state, "completed")
                    pages = document.get("pages")
                    if not isinstance(pages, list):
                        self.fail("Expected page results")
                    self.assertEqual(len(pages), 1)
                    page = json_object(pages[0])
                    self.assertEqual(page.get("index"), 0)
                    text = page.get("markdown")
                    if not isinstance(text, str):
                        self.fail("Expected extracted page text")
                    self.assertEqual(text.strip(), marker(index))
                    self.assertEqual(
                        document.get("summary"),
                        f"## Smoke-test summary\n\n{marker(index)}",
                    )
                del pending[document_id]
            if pending:
                time.sleep(0.1)

        self.assertEqual(len(documents), UPLOAD_COUNT)
        print("Concurrency smoke passed: 10 uploads; 9 completed, 1 expected failure")


if __name__ == "__main__":
    unittest.main()
