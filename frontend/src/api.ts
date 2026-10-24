import type { components } from "./generated/api";

export type ParserKind = components["schemas"]["ParserKind"];
export type DocumentStatus = components["schemas"]["DocumentStatus"];
type GeneratedAcceptedDocument = components["schemas"]["AcceptedDocumentResponse"];
export type AcceptedDocument = Omit<GeneratedAcceptedDocument, "status"> & {
  status: "queued";
};
export type PageContent = components["schemas"]["PageResponse"];
export type CompletedDocument = components["schemas"]["CompletedDocumentResponse"];
export type FailedDocument = components["schemas"]["FailedDocumentResponse"];
export type DocumentResource = components["schemas"]["DocumentResponse"];
export type TrackedDocument = AcceptedDocument | DocumentResource;

export interface DocumentReader {
  getDocument(id: string, signal?: AbortSignal): Promise<DocumentResource>;
}

export interface DocumentClient extends DocumentReader {
  submitDocuments(
    files: readonly File[],
    parser: ParserKind,
    signal?: AbortSignal,
  ): Promise<AcceptedDocument[]>;
}

export type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const API_PATH_PREFIX = "/api/v1/";
const browserFetch: Fetcher = globalThis.fetch.bind(globalThis);
const defaultFetcher: Fetcher = (input, init) => {
  if (typeof input !== "string" || !input.startsWith(API_PATH_PREFIX)) {
    return Promise.reject(new TypeError("Only same-origin API paths are allowed."));
  }
  return browserFetch(input, init);
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export class DocumentApi implements DocumentReader {
  constructor(private readonly fetcher: Fetcher = defaultFetcher) {}

  async submitDocuments(
    files: readonly File[],
    parser: ParserKind,
    signal?: AbortSignal,
  ): Promise<AcceptedDocument[]> {
    const form = new FormData();
    form.set("parser", parser);
    for (const file of files) form.append("files", file);

    const response = await this.fetcher("/api/v1/documents", {
      method: "POST",
      body: form,
      signal,
    });
    const payload: unknown = await response.json();
    if (!response.ok) throw parseApiError(response.status, payload);
    return parseAcceptedBatch(payload);
  }

  async getDocument(id: string, signal?: AbortSignal): Promise<DocumentResource> {
    const response = await this.fetcher(`/api/v1/documents/${encodeURIComponent(id)}`, {
      method: "GET",
      signal,
    });
    const payload: unknown = await response.json();
    if (!response.ok) throw parseApiError(response.status, payload);
    return parseDocument(payload);
  }
}

function parseAcceptedBatch(value: unknown): AcceptedDocument[] {
  const body = record(value);
  if (!Array.isArray(body.documents)) invalidResponse();
  return body.documents.map((document) => {
    const item = record(document);
    const status = string(item, "status");
    if (status !== "queued") invalidResponse();
    return {
      id: string(item, "id"),
      filename: string(item, "filename"),
      parser: parserKind(item.parser),
      status,
      created_at: string(item, "created_at"),
    };
  });
}

function parseDocument(value: unknown): DocumentResource {
  const item = record(value);
  const base = {
    id: string(item, "id"),
    filename: string(item, "filename"),
    parser: parserKind(item.parser),
    created_at: string(item, "created_at"),
    updated_at: string(item, "updated_at"),
    attempts: number(item, "attempts"),
  };
  const status = documentStatus(item.status);
  if (status === "queued" || status === "processing") return { ...base, status };
  const completed_at = string(item, "completed_at");
  if (status === "failed") {
    const error = record(item.error);
    return {
      ...base,
      status,
      completed_at,
      error: { code: string(error, "code"), message: string(error, "message") },
    };
  }
  if (!Array.isArray(item.pages)) invalidResponse();
  return {
    ...base,
    status,
    completed_at,
    pages: item.pages.map((page) => {
      const parsed = record(page);
      return { index: number(parsed, "index"), markdown: string(parsed, "markdown") };
    }),
    summary: string(item, "summary"),
  };
}

function parseApiError(status: number, value: unknown): ApiError {
  try {
    const body = record(value);
    const error = record(body.error);
    return new ApiError(status, string(error, "code"), string(error, "message"));
  } catch {
    return new ApiError(status, "request_failed", "The request failed.");
  }
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) invalidResponse();
  return value as Record<string, unknown>;
}

function string(value: Record<string, unknown>, key: string): string {
  const item = value[key];
  if (typeof item !== "string") invalidResponse();
  return item;
}

function number(value: Record<string, unknown>, key: string): number {
  const item = value[key];
  if (typeof item !== "number" || !Number.isFinite(item)) invalidResponse();
  return item;
}

function parserKind(value: unknown): ParserKind {
  if (value !== "pypdf" && value !== "gemini") invalidResponse();
  return value;
}

function documentStatus(value: unknown): DocumentStatus {
  if (value !== "queued" && value !== "processing" && value !== "completed" && value !== "failed") {
    invalidResponse();
  }
  return value;
}

function invalidResponse(): never {
  throw new Error("The server returned an invalid response.");
}
