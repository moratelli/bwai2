import { useState } from "react";
import type { ChangeEvent, SyntheticEvent } from "react";
import ReactMarkdown from "react-markdown";

import { ApiError, DocumentApi } from "./api";
import type { DocumentClient, ParserKind, ProviderCause, TrackedDocument } from "./api";
import { parserOptions } from "./parserOptions";
import { useDocumentPolling } from "./useDocumentPolling";

const MAX_FILES = 10;
const MAX_FILE_BYTES = 20 * 1024 * 1024;
const defaultClient = new DocumentApi();

interface AppProps {
  client?: DocumentClient;
}

export function App({ client = defaultClient }: AppProps) {
  const [parser, setParser] = useState<ParserKind>("pypdf");
  const [files, setFiles] = useState<File[]>([]);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const { documents, pollError, track } = useDocumentPolling(client);

  const onFilesSelected = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.currentTarget.files ?? []);
    setUploadError(null);
    if (selected.length > MAX_FILES) {
      setFiles([]);
      setSelectionError(`Choose at most ${MAX_FILES} PDF files.`);
      return;
    }
    if (selected.some((file) => file.type !== "application/pdf")) {
      setFiles([]);
      setSelectionError("Choose PDF files only.");
      return;
    }
    const oversized = selected.find((file) => file.size > MAX_FILE_BYTES);
    if (oversized !== undefined) {
      setFiles([]);
      setSelectionError(`${oversized.name} is larger than 20 MiB.`);
      return;
    }
    setFiles(selected);
    setSelectionError(null);
  };

  const submit = async (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (files.length === 0) {
      setSelectionError("Choose at least one PDF file.");
      return;
    }
    setSubmitting(true);
    setUploadError(null);
    try {
      track(await client.submitDocuments(files, parser));
    } catch (error) {
      setUploadError(
        error instanceof ApiError ? error.message : "The upload failed. Please try again.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  const visibleError = selectionError ?? uploadError;

  return (
    <main className="app-shell">
      <header className="hero">
        <p>PDF extraction and summarization</p>
        <h1>Async document processor</h1>
        <p>
          Upload up to ten PDFs, choose how they should be read, and keep this page open while the
          worker processes each file independently.
        </p>
      </header>

      <form className="upload-panel" onSubmit={(event) => void submit(event)}>
        <fieldset className="parser-fieldset">
          <legend>Choose how to read the PDFs</legend>
          <div className="parser-grid">
            {parserOptions.map((option) => (
              <label className="parser-card" key={option.value} htmlFor={`parser-${option.value}`}>
                <input
                  checked={parser === option.value}
                  id={`parser-${option.value}`}
                  name="parser"
                  onChange={() => setParser(option.value)}
                  type="radio"
                  value={option.value}
                />
                <strong>{option.label}</strong>
                <span>{option.description}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <label className="file-label" htmlFor="pdf-files">
          PDF files
        </label>
        <input
          accept="application/pdf,.pdf"
          className="file-input"
          id="pdf-files"
          multiple
          onChange={onFilesSelected}
          type="file"
        />
        <p className="field-help">Maximum 10 files, 20 MiB each.</p>

        {files.length > 0 && (
          <ul aria-label="Selected files" className="selected-files">
            {files.map((file) => (
              <li key={`${file.name}-${file.size}`}>{file.name}</li>
            ))}
          </ul>
        )}

        {visibleError !== null && (
          <p className="alert" role="alert">
            {visibleError}
          </p>
        )}
        <button className="primary-action" disabled={submitting} type="submit">
          {submitting ? "Submitting…" : "Process documents"}
        </button>
        <p className="provider-note">
          Every completed document uses Gemini for its summary, including files extracted locally
          with PyPDF.
        </p>
      </form>

      <section aria-labelledby="results-heading" className="results-section">
        <h2 id="results-heading">Results</h2>
        <p aria-live="polite" className="status-line" role="status">
          {statusMessage(documents, pollError)}
        </p>
        <div className="results-grid">
          {documents.map((document) => (
            <DocumentCard document={document} key={document.id} />
          ))}
        </div>
      </section>
    </main>
  );
}

function DocumentCard({ document }: { document: TrackedDocument }) {
  const wait = document.status === "processing" ? document.retry_wait : null;
  return (
    <article
      aria-labelledby={`document-${document.id}`}
      className="result-card"
      data-status={document.status}
    >
      <header className="result-header">
        <h2 id={`document-${document.id}`}>{document.filename}</h2>
        <p className="status-pill">{wait ? "retry scheduled" : document.status}</p>
      </header>

      {wait && (
        <div className="retry-block" aria-live="polite">
          <h3>Waiting to retry Gemini</h3>
          <CauseDetails
            cause={wait.cause}
            attempts={document.status === "processing" ? document.attempts : 0}
          />
          <p>
            Retry scheduled no earlier than{" "}
            <time dateTime={wait.until}>{new Date(wait.until).toLocaleString()}</time>. Processing
            may start later.
          </p>
        </div>
      )}

      {document.status === "completed" && (
        <>
          <section aria-labelledby={`summary-${document.id}`} className="summary-block">
            <h3 id={`summary-${document.id}`}>Summary</h3>
            <div className="markdown-content">
              <ReactMarkdown>{document.summary}</ReactMarkdown>
            </div>
          </section>
          <div className="page-list">
            {document.pages.map((page) => (
              <details key={page.index}>
                <summary>Page {page.index + 1}</summary>
                <div className="markdown-content">
                  <ReactMarkdown>{page.markdown}</ReactMarkdown>
                </div>
              </details>
            ))}
          </div>
        </>
      )}

      {document.status === "unavailable" && (
        <div className="failure-block">
          <p role="alert">Document not found or expired. Upload it again to process it.</p>
        </div>
      )}

      {document.status === "failed" && (
        <div className="failure-block">
          {document.error.cause && (
            <CauseDetails cause={document.error.cause} attempts={document.attempts} />
          )}
          <p role="alert">{document.error.message}</p>
          {document.error.code === "empty_extraction" && document.parser === "pypdf" && (
            <p>
              Fast text cannot read scanned pages. Try again with Rich Markdown / Gemini, or use a
              text-based PDF. Scan quality affects extraction. The app will not switch parsers
              automatically.
            </p>
          )}
        </div>
      )}
    </article>
  );
}

function CauseDetails({ cause, attempts }: { cause: ProviderCause; attempts: number }) {
  const label = {
    rate_limited: "Gemini API limit reached",
    unavailable: "Gemini temporarily unavailable",
    timeout: "Gemini request timed out",
    connection_error: "Gemini connection failed",
    temporary_failure: "Temporary Gemini failure",
  }[cause.category];
  return (
    <div className="cause-details">
      <strong>{label}</strong>
      <p>
        {cause.stage} · attempt {cause.attempt}
        {cause.http_status != null ? ` · HTTP ${cause.http_status}` : ""}
      </p>
      {cause.attempt < attempts && (
        <p>
          Last confirmed failure on attempt {cause.attempt}; final admitted attempt: {attempts}.
        </p>
      )}
    </div>
  );
}

function statusMessage(documents: readonly TrackedDocument[], pollError: string | null): string {
  if (documents.length === 0) return pollError ?? "No documents submitted yet.";
  const counts = { completed: 0, failed: 0, active: 0, unavailable: 0 };
  for (const document of documents) {
    if (document.status === "queued" || document.status === "processing") counts.active += 1;
    else counts[document.status] += 1;
  }
  const summary = `${counts.completed} completed · ${counts.failed} failed · ${counts.active} active · ${counts.unavailable} unavailable`;
  return pollError === null ? summary : `${summary}. ${pollError}`;
}
