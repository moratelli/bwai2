import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Provider, createStore } from "jotai";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { AcceptedDocument, CompletedDocument, DocumentClient, DocumentResource } from "./api";
import { ApiError } from "./api";
import { documentsAtom } from "./pollingAtoms";

const accepted: AcceptedDocument = {
  id: "00000000-0000-4000-8000-000000000001",
  filename: "report.pdf",
  parser: "pypdf",
  status: "queued",
  created_at: "2026-10-23T12:00:00Z",
};

function fakeClient(): DocumentClient & {
  submitDocuments: ReturnType<typeof vi.fn<DocumentClient["submitDocuments"]>>;
  getDocument: ReturnType<typeof vi.fn<DocumentClient["getDocument"]>>;
} {
  return {
    submitDocuments: vi.fn<DocumentClient["submitDocuments"]>().mockResolvedValue([accepted]),
    getDocument: vi.fn<DocumentClient["getDocument"]>(),
  };
}

function renderApp(client = fakeClient(), documents: DocumentResource[] = []) {
  const store = createStore();
  store.set(documentsAtom, documents);
  return {
    client,
    store,
    ...render(
      <Provider store={store}>
        <App client={client} />
      </Provider>,
    ),
  };
}

function pdf(name = "report.pdf") {
  return new File(["%PDF-1.7"], name, { type: "application/pdf" });
}

function completedDocument(markdown = "# Page result"): CompletedDocument {
  return {
    ...accepted,
    status: "completed",
    updated_at: "2026-10-23T12:01:00Z",
    completed_at: "2026-10-23T12:01:00Z",
    attempts: 1,
    summary: "Useful result.",
    pages: [{ index: 0, markdown }],
  };
}

describe("App", () => {
  it("shows intent-based provider guidance with Fast text selected by default", () => {
    renderApp();

    expect(screen.getByRole("heading", { name: "Async document processor" })).toBeVisible();
    expect(screen.getByRole("radio", { name: /Fast text \/ PyPDF/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Rich Markdown \/ Gemini/ })).not.toBeChecked();
    expect(screen.getAllByRole("radio")).toHaveLength(2);
    expect(screen.queryByRole("radio", { name: /Mistral/ })).toBeNull();
    expect(screen.queryByText(/Mistral/)).toBeNull();
    expect(screen.getByText(/does not OCR scanned pages/i)).toBeVisible();
    expect(screen.getByText(/scan quality affects results/i)).toBeVisible();
    expect(screen.getByText(/every completed document uses Gemini for its summary/i)).toBeVisible();
  });

  it("uploads selected PDFs with the chosen parser using the keyboard", async () => {
    const user = userEvent.setup();
    const client = fakeClient();
    renderApp(client);

    await user.click(screen.getByRole("radio", { name: /Rich Markdown \/ Gemini/ }));
    fireEvent.change(screen.getByLabelText("PDF files"), {
      target: { files: [pdf()] },
    });
    const submit = screen.getByRole("button", { name: "Process documents" });
    submit.focus();
    await user.keyboard("{Enter}");

    await waitFor(() => expect(client.submitDocuments).toHaveBeenCalledTimes(1));
    expect(client.submitDocuments).toHaveBeenCalledWith([expect.any(File)], "gemini");
    expect(screen.getByRole("heading", { name: "report.pdf" })).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("1 document queued");
  });

  it("rejects obvious invalid file selections before submission", () => {
    const client = fakeClient();
    renderApp(client);
    const input = screen.getByLabelText("PDF files");

    fireEvent.change(input, {
      target: { files: [new File(["notes"], "notes.txt", { type: "text/plain" })] },
    });

    expect(screen.getByRole("alert")).toHaveTextContent("Choose PDF files only");
    expect(client.submitDocuments).not.toHaveBeenCalled();
  });

  it("shows a safe upload error without discarding the selected file", async () => {
    const user = userEvent.setup();
    const client = fakeClient();
    client.submitDocuments.mockRejectedValue(
      new ApiError(503, "provider_unavailable", "Gemini is not configured."),
    );
    renderApp(client);

    fireEvent.change(screen.getByLabelText("PDF files"), {
      target: { files: [pdf()] },
    });
    await user.click(screen.getByRole("button", { name: "Process documents" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Gemini is not configured.");
    expect(screen.getByText("report.pdf")).toBeVisible();
  });

  it("renders completed and failed documents independently", () => {
    const failed: DocumentResource = {
      ...accepted,
      id: "00000000-0000-4000-8000-000000000002",
      filename: "broken.pdf",
      status: "failed",
      updated_at: "2026-10-23T12:01:00Z",
      completed_at: "2026-10-23T12:01:00Z",
      attempts: 1,
      error: { code: "invalid_pdf", message: "The PDF is corrupt or unsupported." },
    };
    renderApp(fakeClient(), [completedDocument(), failed]);

    expect(screen.getByRole("heading", { name: "report.pdf" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Summary" })).toBeVisible();
    expect(screen.getByText("Useful result.")).toBeVisible();
    expect(screen.getByText("Page 1")).toBeVisible();
    expect(screen.getByRole("heading", { name: "broken.pdf" })).toBeVisible();
    expect(screen.getByText("The PDF is corrupt or unsupported.")).toBeVisible();
  });

  it("does not execute raw HTML returned in Markdown", () => {
    const { container } = renderApp(fakeClient(), [
      completedDocument("Before<script>window.hacked = true</script>After"),
    ]);

    expect(container.querySelector("script")).toBeNull();
    expect(window).not.toHaveProperty("hacked", true);
  });

  it("suggests an explicit Gemini retry for Fast text without switching parsers", async () => {
    const emptyFailure: DocumentResource = {
      ...accepted,
      status: "failed",
      updated_at: "2026-10-23T12:01:00Z",
      completed_at: "2026-10-23T12:01:00Z",
      attempts: 1,
      error: {
        code: "empty_extraction",
        message: "No extractable text was found.",
      },
    };
    const client = fakeClient();
    renderApp(client, [emptyFailure]);

    expect(screen.getByText(/try again with Rich Markdown \/ Gemini/i)).toBeVisible();
    expect(screen.getByText(/Fast text cannot read scanned pages/i)).toBeVisible();
    expect(screen.getByRole("radio", { name: /Fast text \/ PyPDF/ })).toBeChecked();
    await act(async () => undefined);
    expect(client.submitDocuments).not.toHaveBeenCalled();
  });

  it("does not suggest retrying Gemini when Gemini already returned no text", async () => {
    const emptyFailure: DocumentResource = {
      ...accepted,
      parser: "gemini",
      status: "failed",
      updated_at: "2026-10-23T12:01:00Z",
      completed_at: "2026-10-23T12:01:00Z",
      attempts: 1,
      error: { code: "empty_extraction", message: "No readable text was extracted." },
    };
    const client = fakeClient();
    renderApp(client, [emptyFailure]);

    expect(screen.getByText("No readable text was extracted.")).toBeVisible();
    expect(screen.queryByText(/try again with/i)).toBeNull();
    expect(screen.getByRole("radio", { name: /Fast text \/ PyPDF/ })).toBeChecked();
    await act(async () => undefined);
    expect(client.submitDocuments).not.toHaveBeenCalled();
  });
});
