import { act, renderHook } from "@testing-library/react";
import { Provider, createStore } from "jotai";
import type { PropsWithChildren } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AcceptedDocument, DocumentResource, DocumentReader } from "./api";
import { ApiError } from "./api";
import { useDocumentPolling } from "./useDocumentPolling";

const first: AcceptedDocument = {
  id: "00000000-0000-4000-8000-000000000001",
  filename: "one.pdf",
  parser: "pypdf",
  status: "queued",
  created_at: "2026-10-23T12:00:00Z",
};
const second: AcceptedDocument = {
  ...first,
  id: "00000000-0000-4000-8000-000000000002",
  filename: "two.pdf",
};

function resource(
  accepted: AcceptedDocument,
  status: "processing" | "completed" | "failed",
): DocumentResource {
  const base = {
    ...accepted,
    status,
    updated_at: "2026-10-23T12:01:00Z",
    attempts: 1,
  };
  if (status === "completed") {
    return {
      ...base,
      status,
      completed_at: "2026-10-23T12:01:00Z",
      pages: [{ index: 0, markdown: "Text" }],
      summary: "Summary",
    };
  }
  if (status === "failed") {
    return {
      ...base,
      status,
      completed_at: "2026-10-23T12:01:00Z",
      error: { code: "invalid_pdf", message: "Safe failure." },
    };
  }
  return { ...base, status };
}

function createWrapper() {
  const store = createStore();
  return function Wrapper({ children }: PropsWithChildren) {
    return <Provider store={store}>{children}</Provider>;
  };
}

describe("useDocumentPolling", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("tracks an accepted batch and lets documents finish independently", async () => {
    const calls = new Map<string, number>();
    const reader: DocumentReader = {
      getDocument: vi.fn(async (id: string) => {
        const count = (calls.get(id) ?? 0) + 1;
        calls.set(id, count);
        if (id === first.id) return resource(first, "completed");
        return resource(second, count === 1 ? "processing" : "failed");
      }),
    };
    const { result } = renderHook(
      () => useDocumentPolling(reader, { intervalMs: 1_000, maxIntervalMs: 8_000 }),
      { wrapper: createWrapper() },
    );

    act(() => result.current.track([first, second]));
    expect(result.current.documents.map((document) => document.status)).toEqual([
      "queued",
      "queued",
    ]);

    await act(() => vi.advanceTimersByTimeAsync(1_000));
    expect(result.current.documents.map((document) => document.status)).toEqual([
      "completed",
      "processing",
    ]);

    await act(() => vi.advanceTimersByTimeAsync(1_000));
    expect(result.current.documents.map((document) => document.status)).toEqual([
      "completed",
      "failed",
    ]);
    expect(reader.getDocument).toHaveBeenCalledTimes(3);
  });

  it("stops polling a missing document without a temporary-error banner", async () => {
    const getDocument = vi
      .fn<DocumentReader["getDocument"]>()
      .mockRejectedValue(new ApiError(404, "document_not_found", "Not found."));
    const { result } = renderHook(() => useDocumentPolling({ getDocument }), {
      wrapper: createWrapper(),
    });
    act(() => result.current.track([first]));
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    expect(result.current.documents[0]?.status).toBe("unavailable");
    expect(result.current.pollError).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(getDocument).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);

    getDocument.mockResolvedValueOnce(resource(second, "completed"));
    act(() => result.current.track([second]));
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    expect(result.current.documents.map(({ status }) => status)).toEqual([
      "unavailable",
      "completed",
    ]);
    expect(getDocument).toHaveBeenCalledTimes(2);
    expect(getDocument).toHaveBeenLastCalledWith(second.id, expect.any(AbortSignal));
  });

  it("keeps missing, completed, and temporarily failing siblings independent", async () => {
    const third = { ...first, id: "00000000-0000-4000-8000-000000000003" };
    let thirdCalls = 0;
    const getDocument = vi.fn(async (id: string) => {
      if (id === first.id) throw new ApiError(404, "document_not_found", "Not found.");
      if (id === second.id) return resource(second, "completed");
      if (++thirdCalls === 1) throw new ApiError(503, "unavailable", "Unavailable.");
      return resource(third, "completed");
    });
    const { result } = renderHook(() => useDocumentPolling({ getDocument }), {
      wrapper: createWrapper(),
    });
    act(() => result.current.track([first, second, third]));
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    expect(result.current.documents.map(({ status }) => status)).toEqual([
      "unavailable",
      "completed",
      "queued",
    ]);
    expect(result.current.pollError).toContain("Retrying");
    await act(() => vi.advanceTimersByTimeAsync(1_999));
    expect(getDocument).toHaveBeenCalledTimes(3);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(result.current.documents.map(({ status }) => status)).toEqual([
      "unavailable",
      "completed",
      "completed",
    ]);
    expect(result.current.pollError).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(getDocument).toHaveBeenCalledTimes(4);
  });

  it("ignores a late 404 after clearing the tracked documents", async () => {
    let rejectPending: ((reason: unknown) => void) | undefined;
    const getDocument = vi.fn(
      () =>
        new Promise<DocumentResource>((_resolve, reject) => {
          rejectPending = reject;
        }),
    );
    const { result } = renderHook(() => useDocumentPolling({ getDocument }), {
      wrapper: createWrapper(),
    });
    act(() => result.current.track([first]));
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    act(() => result.current.clear());
    await act(async () => rejectPending?.(new ApiError(404, "document_not_found", "Not found.")));
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(result.current.documents).toEqual([]);
    expect(result.current.pollError).toBeNull();
    expect(getDocument).toHaveBeenCalledTimes(1);
  });

  it("backs off after temporary failures and preserves the last state", async () => {
    const getDocument = vi
      .fn<(id: string, signal?: AbortSignal) => Promise<DocumentResource>>()
      .mockRejectedValueOnce(new TypeError("temporary network failure"))
      .mockResolvedValueOnce(resource(first, "completed"));
    const { result } = renderHook(
      () => useDocumentPolling({ getDocument }, { intervalMs: 1_000, maxIntervalMs: 8_000 }),
      { wrapper: createWrapper() },
    );

    act(() => result.current.track([first]));
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    expect(result.current.documents[0]?.status).toBe("queued");
    expect(result.current.pollError).toBe("Results are temporarily unavailable. Retrying…");

    await act(() => vi.advanceTimersByTimeAsync(1_999));
    expect(getDocument).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(getDocument).toHaveBeenCalledTimes(2);
    expect(result.current.documents[0]?.status).toBe("completed");
    expect(result.current.pollError).toBeNull();
  });

  it("aborts requests and clears timers on unmount", async () => {
    let signal: AbortSignal | undefined;
    const getDocument = vi.fn(
      async (_id: string, requestSignal?: AbortSignal): Promise<DocumentResource> => {
        signal = requestSignal;
        return new Promise<DocumentResource>(() => undefined);
      },
    );
    const { result, unmount } = renderHook(
      () => useDocumentPolling({ getDocument }, { intervalMs: 1_000, maxIntervalMs: 8_000 }),
      { wrapper: createWrapper() },
    );

    act(() => result.current.track([first]));
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    expect(signal?.aborted).toBe(false);

    unmount();

    expect(signal?.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(getDocument).toHaveBeenCalledTimes(1);
  });
});
