import { describe, expect, it, vi } from "vitest";

import { DocumentApi } from "./api";

const acceptedBody = {
  documents: [
    {
      id: "00000000-0000-4000-8000-000000000001",
      filename: "report.pdf",
      parser: "pypdf",
      status: "queued",
      created_at: "2026-10-23T12:00:00Z",
    },
  ],
};

function jsonResponse(body: object, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("DocumentApi", () => {
  it("submits a multipart batch with the selected parser", async () => {
    const fetcher = vi
      .fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValue(jsonResponse(acceptedBody, 202));
    const api = new DocumentApi(fetcher);
    const files = [new File(["%PDF-one"], "one.pdf", { type: "application/pdf" })];

    const accepted = await api.submitDocuments(files, "pypdf");

    expect(accepted).toEqual(acceptedBody.documents);
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [url, request] = fetcher.mock.calls[0];
    expect(url).toBe("/api/v1/documents");
    expect(request?.method).toBe("POST");
    expect(request?.body).toBeInstanceOf(FormData);
    const form = request?.body as FormData;
    expect(form.get("parser")).toBe("pypdf");
    expect(form.getAll("files")).toEqual(files);
  });

  it("retrieves a typed terminal document", async () => {
    const completed = {
      id: acceptedBody.documents[0].id,
      filename: "report.pdf",
      parser: "pypdf",
      status: "completed",
      created_at: "2026-10-23T12:00:00Z",
      updated_at: "2026-10-23T12:01:00Z",
      completed_at: "2026-10-23T12:01:00Z",
      attempts: 1,
      pages: [{ index: 0, markdown: "# Result" }],
      summary: "Summary",
    };
    const fetcher = vi
      .fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValue(jsonResponse(completed));

    const result = await new DocumentApi(fetcher).getDocument(completed.id);

    expect(result).toEqual(completed);
    expect(fetcher).toHaveBeenCalledWith(
      `/api/v1/documents/${completed.id}`,
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("rejects the retired parser in submission responses", async () => {
    const fetcher = vi
      .fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValue(
        jsonResponse(
          {
            documents: acceptedBody.documents.map((document) => ({
              ...document,
              parser: "mistral",
            })),
          },
          202,
        ),
      );

    await expect(
      new DocumentApi(fetcher).submitDocuments(
        [new File(["%PDF"], "one.pdf", { type: "application/pdf" })],
        "pypdf",
      ),
    ).rejects.toThrow("invalid response");
  });

  it("rejects the retired parser in polled responses", async () => {
    const queued = {
      ...acceptedBody.documents[0],
      parser: "mistral",
      updated_at: "2026-10-23T12:01:00Z",
      attempts: 0,
    };
    const fetcher = vi
      .fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValue(jsonResponse(queued));

    await expect(new DocumentApi(fetcher).getDocument(queued.id)).rejects.toThrow(
      "invalid response",
    );
  });

  it("throws the safe API error envelope", async () => {
    const fetcher = vi
      .fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValue(
        jsonResponse(
          { error: { code: "provider_unavailable", message: "Provider unavailable." } },
          503,
        ),
      );

    await expect(
      new DocumentApi(fetcher).submitDocuments(
        [new File(["%PDF"], "one.pdf", { type: "application/pdf" })],
        "gemini",
      ),
    ).rejects.toMatchObject({
      status: 503,
      code: "provider_unavailable",
      message: "Provider unavailable.",
    });
  });

  it("rejects malformed success payloads", async () => {
    const fetcher = vi
      .fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>()
      .mockResolvedValue(jsonResponse({ documents: [{ status: "surprise" }] }, 202));

    await expect(
      new DocumentApi(fetcher).submitDocuments(
        [new File(["%PDF"], "one.pdf", { type: "application/pdf" })],
        "pypdf",
      ),
    ).rejects.toThrow("invalid response");
  });
});
