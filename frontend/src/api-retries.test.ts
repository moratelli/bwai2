import { describe, expect, it, vi } from "vitest";

import { DocumentApi } from "./api";

const base = {
  id: "00000000-0000-4000-8000-000000000001",
  filename: "synthetic.pdf",
  parser: "pypdf",
  status: "processing",
  created_at: "2026-10-25T18:00:00Z",
  updated_at: "2026-10-25T18:00:00Z",
  attempts: 1,
};
const cause = {
  provider: "gemini",
  category: "rate_limited",
  stage: "summarization",
  attempt: 1,
  http_status: 429,
};
const until = "2026-10-25T18:01:00Z";

async function parse(payload: object) {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }));
  return new DocumentApi(fetcher).getDocument(base.id);
}

function failed(detail: unknown, code = "retry_exhausted") {
  return {
    ...base,
    status: "failed",
    attempts: 3,
    completed_at: until,
    error: { code, message: "Safe failure.", cause: detail },
  };
}

describe("optional provider result metadata", () => {
  it.each([
    ["rate_limited", 429],
    ["unavailable", 503],
    ["temporary_failure", 408],
    ["timeout", undefined],
    ["connection_error", undefined],
  ])("retains validated %s causes and retry deadlines", async (category, http_status) => {
    const detail = { ...cause, category, http_status };
    const payload = { ...base, retry_wait: { until, cause: detail } };
    expect(await parse(payload)).toEqual(payload);
    expect(await parse(failed(detail))).toEqual(failed(detail));
  });

  it("keeps the earlier failed attempt distinct from the current count", async () => {
    expect(await parse(failed(cause))).toMatchObject({
      attempts: 3,
      error: { cause: { attempt: 1 } },
    });
  });

  it.each([
    "PRIVATE_CANARY",
    [],
    {},
    { ...cause, provider: "other" },
    { ...cause, category: "unknown" },
    { ...cause, stage: "PRIVATE_CANARY" },
    { ...cause, attempt: 0 },
    { ...cause, attempt: true },
    { ...cause, attempt: 1.5 },
    { ...cause, attempt: 4 },
    { ...cause, http_status: 503 },
    { ...cause, http_status: "429" },
  ])("rejects malformed provider evidence with a safe error: %j", async (detail) => {
    await expect(parse({ ...base, retry_wait: { until, cause: detail } })).rejects.toThrow(
      "The server returned an invalid response.",
    );
    await expect(parse(failed(detail))).rejects.toThrow("The server returned an invalid response.");
  });

  it.each([undefined, null])(
    "accepts an absent cause, but not an incomplete wait: %j",
    async (detail) => {
      expect(await parse(failed(detail))).not.toHaveProperty("error.cause");
      await expect(parse({ ...base, retry_wait: { until, cause: detail } })).rejects.toThrow(
        "The server returned an invalid response.",
      );
    },
  );

  it.each([
    "PRIVATE_CANARY",
    [],
    {},
    { until: "invalid", cause },
    { until: "2026-10-25T18:01:00", cause },
    { until: "2026-02-30T18:01:00Z", cause },
    { until, cause: { ...cause, attempt: 2 } },
  ])("rejects malformed waiting metadata: %j", async (retry_wait) => {
    await expect(parse({ ...base, retry_wait })).rejects.toThrow(
      "The server returned an invalid response.",
    );
  });

  it("accepts a legacy response and clears waiting on resumed or terminal results", async () => {
    expect(await parse(base)).toEqual(base);
    expect(await parse({ ...base, retry_wait: null })).toEqual(base);
    expect(
      await parse({ ...base, status: "queued", retry_wait: { until, cause } }),
    ).not.toHaveProperty("retry_wait");
    const terminal = await parse({ ...failed(cause), retry_wait: { until, cause } });
    expect(terminal).not.toHaveProperty("retry_wait");
    expect(await parse(failed(cause, "invalid_pdf"))).not.toHaveProperty("error.cause");
  });

  it("discards arbitrary extra provider content", async () => {
    const result = await parse({
      ...base,
      retry_wait: { until, cause: { ...cause, raw: "PRIVATE_CANARY" }, raw: "PRIVATE_CANARY" },
    });
    expect(result).toEqual({ ...base, retry_wait: { until, cause } });
    expect(JSON.stringify(result)).not.toContain("PRIVATE_CANARY");
  });
});
