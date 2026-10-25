import { expect, test } from "@playwright/test";

import type { AcceptedDocument, DocumentResource } from "../src/api";

// Scripted API responses test UI behavior only; no backend job or provider request is made.
test("scripted responses: retries and mixed outcomes stay distinct", async ({ page }) => {
  const created_at = "2026-10-25T18:00:00Z";
  const until = "2026-10-25T18:01:00Z";
  const documents: AcceptedDocument[] = ["wait", "done", "failed", "missing"].map(
    (name, index) => ({
      id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
      filename: `scripted-${name}.pdf`,
      parser: "pypdf",
      status: "queued",
      created_at,
    }),
  );
  const common = { updated_at: created_at, attempts: 1 };
  const cause = {
    provider: "gemini",
    category: "rate_limited",
    stage: "summarization",
    attempt: 1,
    http_status: 429,
  } as const;
  const waiting: DocumentResource = {
    ...documents[0],
    ...common,
    status: "processing",
    retry_wait: { until, cause },
  };
  const completed: DocumentResource = {
    ...documents[1],
    ...common,
    status: "completed",
    completed_at: until,
    pages: [{ index: 0, markdown: "Scripted page." }],
    summary: "Scripted successful sibling.",
  };
  const failed: DocumentResource = {
    ...documents[2],
    ...common,
    status: "failed",
    attempts: 3,
    completed_at: until,
    error: {
      code: "retry_exhausted",
      message: "Last confirmed failure on attempt 1: Gemini API limit reached.",
      cause,
    },
  };
  const replies = new Map<string, DocumentResource>([
    [waiting.id, waiting],
    [completed.id, completed],
    [failed.id, failed],
  ]);
  const calls = new Map<string, number>();
  await page.route("**/api/v1/documents", (route) =>
    route.fulfill({ status: 202, json: { documents } }),
  );
  await page.route("**/api/v1/documents/*", (route) => {
    const id = route.request().url().split("/").at(-1) ?? "";
    calls.set(id, (calls.get(id) ?? 0) + 1);
    const result = replies.get(id);
    return route.fulfill({
      status: result ? 200 : 404,
      json: result ?? { error: { code: "document_not_found", message: "Not found." } },
    });
  });
  await page.goto("/");
  await page.getByLabel("PDF files").setInputFiles({
    name: "scripted.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-scripted"),
  });
  await page.getByRole("button", { name: "Process documents" }).click();
  await expect(page.getByRole("status")).toHaveText(
    "1 completed · 1 failed · 1 active · 1 unavailable",
  );
  const card = page.getByRole("article", { name: "scripted-wait.pdf" });
  await expect(card.getByRole("heading", { name: "Waiting to retry Gemini" })).toBeVisible();
  await expect(card.locator("time")).toHaveAttribute("datetime", until);
  await expect(card.getByText(/Processing may start later/)).toBeVisible();
  await expect(page.getByText("Scripted successful sibling.")).toBeVisible();
  await expect(
    page
      .getByRole("article", { name: "scripted-failed.pdf" })
      .getByText(/^Last confirmed failure on attempt 1;/),
  ).toBeVisible();
  await expect(page.getByRole("article", { name: "scripted-missing.pdf" })).toHaveAttribute(
    "data-status",
    "unavailable",
  );
  replies.set(waiting.id, { ...documents[0], ...common, status: "processing", attempts: 2 });
  await expect(card.getByText("processing", { exact: true })).toBeVisible();
  await expect(card.locator("time")).toHaveCount(0);
  replies.set(waiting.id, {
    ...completed,
    id: waiting.id,
    filename: waiting.filename,
    attempts: 2,
  });
  await expect(page.getByRole("status")).toHaveText(
    "2 completed · 1 failed · 0 active · 1 unavailable",
  );
  for (const document of documents.slice(1)) expect(calls.get(document.id)).toBe(1);
});
