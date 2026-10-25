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
export type ProviderCause = components["schemas"]["ProviderCauseResponse"];
export type RetryWait = components["schemas"]["RetryWaitResponse"];
export type DocumentResource = components["schemas"]["DocumentResponse"];
export type UnavailableDocument = Omit<AcceptedDocument, "status"> & {
  status: "unavailable";
};
export type TrackedDocument = AcceptedDocument | DocumentResource | UnavailableDocument;

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
    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => null);
      throw parseApiError(response.status, payload);
    }
    const payload: unknown = await response.json();
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
  if (status === "queued") return { ...base, status };
  if (status === "processing") {
    const retry_wait = parseRetryWait(item.retry_wait, base.attempts);
    return { ...base, status, ...(retry_wait ? { retry_wait } : {}) };
  }
  const completed_at = string(item, "completed_at");
  if (status === "failed") {
    const error = record(item.error);
    const code = string(error, "code");
    const cause =
      code === "retry_exhausted" ? parseProviderCause(error.cause, base.attempts) : undefined;
    return {
      ...base,
      status,
      completed_at,
      error: { code, message: string(error, "message"), ...(cause ? { cause } : {}) },
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

function parseProviderCause(value: unknown, attempts: number): ProviderCause | undefined {
  const item = optionalRecord(value);
  if (!item || item.provider !== "gemini") return undefined;
  const { category, stage, attempt, http_status } = item;
  if (
    category !== "rate_limited" &&
    category !== "unavailable" &&
    category !== "timeout" &&
    category !== "connection_error" &&
    category !== "temporary_failure"
  )
    return undefined;
  if (stage !== "extraction" && stage !== "summarization") return undefined;
  if (
    typeof attempt !== "number" ||
    !Number.isSafeInteger(attempt) ||
    attempt < 1 ||
    attempt > attempts
  )
    return undefined;
  if (http_status != null) {
    if (
      typeof http_status !== "number" ||
      !Number.isInteger(http_status) ||
      http_status < 100 ||
      http_status > 599
    )
      return undefined;
    if (category === "rate_limited" && http_status !== 429) return undefined;
    if (category === "unavailable" && http_status < 500) return undefined;
    if (category === "temporary_failure" && http_status !== 408 && http_status !== 409)
      return undefined;
    if (
      (category === "timeout" || category === "connection_error") &&
      (http_status === 429 || http_status >= 500)
    )
      return undefined;
  }
  return {
    provider: "gemini",
    category,
    stage,
    attempt,
    ...(http_status != null ? { http_status: http_status as number } : {}),
  };
}

function parseRetryWait(value: unknown, attempts: number): RetryWait | undefined {
  const item = optionalRecord(value);
  if (!item || !validRetryTime(item.until)) return undefined;
  const cause = parseProviderCause(item.cause, attempts);
  if (!cause || cause.attempt !== attempts) return undefined;
  return { until: item.until, cause };
}

function validRetryTime(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const match = /^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.exec(
    value,
  );
  if (!match || !Number.isFinite(Date.parse(value))) return false;
  const [year, month, day] = match.slice(1, 4).map(Number);
  return (
    year > 0 &&
    month >= 1 &&
    month <= 12 &&
    day >= 1 &&
    day <= new Date(Date.UTC(year, month, 0)).getUTCDate()
  );
}

function optionalRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
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
