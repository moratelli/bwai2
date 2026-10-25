import { expect, test } from "@playwright/test";
import { fileURLToPath } from "node:url";

const samplePdf = fileURLToPath(new URL("../../.tmp/playwright/sample.pdf", import.meta.url));

test("uploads a PDF and renders its completed result", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Async document processor" })).toBeVisible();
  await expect(page.getByRole("radio", { name: /Fast text \/ PyPDF/ })).toBeChecked();

  await page.getByLabel("PDF files").setInputFiles(samplePdf);
  await expect(
    page.getByRole("list", { name: "Selected files" }).getByText("sample.pdf"),
  ).toBeVisible();

  const uploadResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" && response.url().endsWith("/api/v1/documents"),
  );
  await page.getByRole("button", { name: "Process documents" }).click();
  const uploadResponse = await uploadResponsePromise;
  expect(uploadResponse.status(), await uploadResponse.text()).toBe(202);

  const result = page.locator("article.result-card").filter({ hasText: "sample.pdf" });
  await expect(result).toBeVisible();
  await expect(result).toHaveAttribute("data-status", "completed", {
    timeout: 90_000,
  });
  await expect(page.getByRole("status")).toHaveText(
    "1 completed · 0 failed · 0 active · 0 unavailable",
  );
  await expect(result.getByRole("heading", { name: "Smoke-test summary" })).toBeVisible();

  const firstPage = result.locator("details").first();
  await firstPage.getByText("Page 1", { exact: true }).click();
  await expect(
    firstPage.getByText("Async document processor smoke test", { exact: true }),
  ).toBeVisible();
});
