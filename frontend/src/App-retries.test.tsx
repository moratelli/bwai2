import { act, fireEvent, render, screen } from "@testing-library/react";
import { Provider, createStore } from "jotai";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type {
  AcceptedDocument,
  DocumentClient,
  DocumentResource,
  ProviderCause,
  TrackedDocument,
} from "./api";
import { documentsAtom, pollingErrorAtom } from "./pollingAtoms";

const accepted: AcceptedDocument = {
  id: "first",
  filename: "first.pdf",
  parser: "pypdf",
  status: "queued",
  created_at: "2026-10-25T18:00:00Z",
};
const cause: ProviderCause = {
  provider: "gemini",
  category: "rate_limited",
  stage: "summarization",
  attempt: 1,
  http_status: 429,
};
const until = "2026-10-25T18:01:00Z";
const waiting: DocumentResource = {
  ...accepted,
  status: "processing",
  updated_at: accepted.created_at,
  attempts: 1,
  retry_wait: { until, cause },
};
const completed: DocumentResource = {
  ...accepted,
  id: "done",
  filename: "done.pdf",
  status: "completed",
  updated_at: until,
  completed_at: until,
  attempts: 1,
  pages: [{ index: 0, markdown: "Good page" }],
  summary: "Good summary",
};

function renderTracked(documents: TrackedDocument[]) {
  const store = createStore();
  store.set(documentsAtom, documents);
  const client: DocumentClient = {
    getDocument: vi.fn<DocumentClient["getDocument"]>(),
    submitDocuments: vi
      .fn<DocumentClient["submitDocuments"]>()
      .mockResolvedValue([{ ...accepted, id: "new", filename: "new.pdf" }]),
  };
  return {
    store,
    client,
    ...render(
      <Provider store={store}>
        <App client={client} />
      </Provider>,
    ),
  };
}

function failed(detail?: ProviderCause): DocumentResource {
  return {
    ...accepted,
    status: "failed",
    attempts: 3,
    updated_at: until,
    completed_at: until,
    error: {
      code: "retry_exhausted",
      message: "Processing failed after 3 attempts.",
      ...(detail ? { cause: detail } : {}),
    },
  };
}

afterEach(() => vi.useRealTimers());

describe("retry and outcome presentation", () => {
  it("shows an active retry and its fixed not-before time, including after it has passed", () => {
    vi.useFakeTimers();
    vi.setSystemTime("2026-10-25T18:00:00Z");
    const { store, container } = renderTracked([waiting]);
    expect(screen.getByText("Waiting to retry Gemini")).toBeVisible();
    expect(screen.getByText("Gemini API limit reached")).toBeVisible();
    expect(screen.getByText(/no earlier than/)).toBeVisible();
    expect(container.querySelector("time")).toHaveAttribute("datetime", until);
    expect(screen.getByRole("status")).toHaveTextContent(
      "0 completed · 0 failed · 1 active · 0 unavailable",
    );
    vi.setSystemTime("2026-10-25T18:02:00Z");
    act(() => store.set(documentsAtom, [{ ...waiting }]));
    expect(screen.getByText(/Processing may start later/)).toBeVisible();
    expect(container.querySelector("time")).toHaveAttribute("datetime", until);
    act(() =>
      store.set(documentsAtom, [
        { ...accepted, status: "processing", updated_at: until, attempts: 2 },
      ]),
    );
    expect(screen.queryByText("Waiting to retry Gemini")).toBeNull();
    expect(container.querySelector("time")).toBeNull();
    expect(screen.getByText("processing")).toBeVisible();
  });

  it.each([
    ["unavailable", "Gemini temporarily unavailable"],
    ["timeout", "Gemini request timed out"],
    ["connection_error", "Gemini connection failed"],
    ["temporary_failure", "Temporary Gemini failure"],
  ] as const)("shows a confirmed %s without calling the PDF invalid", (category, label) => {
    renderTracked([
      failed({
        ...cause,
        category,
        http_status: category === "unavailable" ? 503 : undefined,
        attempt: 3,
        stage: "extraction",
      }),
    ]);
    expect(screen.getByText(label)).toBeVisible();
    expect(screen.getByText(/^extraction · attempt 3/)).toBeVisible();
    expect(screen.queryByText(/PDF is.*invalid/)).toBeNull();
  });

  it("labels earlier evidence and never infers a cause from generic retry exhaustion", () => {
    const { store } = renderTracked([failed(cause)]);
    expect(screen.getByText(/^Last confirmed failure on attempt 1/)).toBeVisible();
    act(() => store.set(documentsAtom, [failed()]));
    expect(screen.queryByText("Gemini API limit reached")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Processing failed after 3 attempts.");
  });

  it("keeps mixed outcome counts visible during polling errors and later submissions", async () => {
    const { store } = renderTracked([
      waiting,
      completed,
      { ...failed(cause), id: "failed", filename: "failed.pdf" },
      { ...accepted, id: "missing", status: "unavailable" },
      { ...accepted, id: "queued" },
    ]);
    act(() => store.set(pollingErrorAtom, "Results are temporarily unavailable. Retrying…"));
    expect(screen.getByRole("status")).toHaveTextContent(
      "1 completed · 1 failed · 2 active · 1 unavailable",
    );
    expect(screen.getByRole("status")).toHaveTextContent("Results are temporarily unavailable.");
    expect(screen.getByText("Good summary")).toBeVisible();
    fireEvent.change(screen.getByLabelText("PDF files"), {
      target: { files: [new File(["%PDF"], "new.pdf", { type: "application/pdf" })] },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Process documents" }));
    });
    expect(screen.getByRole("status")).toHaveTextContent(
      "1 completed · 1 failed · 3 active · 1 unavailable",
    );
    expect(screen.getByText("Good summary")).toBeVisible();
    expect(screen.getByRole("heading", { name: "new.pdf" })).toBeVisible();
  });
});
