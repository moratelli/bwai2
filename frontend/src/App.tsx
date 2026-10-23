import { useState } from "react";
import type { ChangeEvent, SyntheticEvent } from "react";
import ReactMarkdown from "react-markdown";

import { ApiError, DocumentApi } from "./api";
import type { DocumentClient, ParserKind, TrackedDocument } from "./api";
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
    <main>
      <header>
        <p>PDF extraction and summarization</p>
        <h1>Async document processor</h1>
        <p>
          Upload up to ten PDFs, choose how they should be read, and keep this page open while the
          worker processes each file independently.
        </p>
      </header>

      <form onSubmit={(event) => void submit(event)}>
        <fieldset>
          <legend>Choose how to read the PDFs</legend>
          <div className="parser-grid">
            {parserOptions.map((option) => (
              <label key={option.value} htmlFor={`parser-${option.value}`}>
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

        <label htmlFor="pdf-files">PDF files</label>
        <input
          accept="application/pdf,.pdf"
          id="pdf-files"
          multiple
          onChange={onFilesSelected}
          type="file"
        />
        <p>Maximum 10 files, 20 MiB each.</p>

        {files.length > 0 && (
          <ul aria-label="Selected files">
            {files.map((file) => (
              <li key={`${file.name}-${file.size}`}>{file.name}</li>
            ))}
          </ul>
        )}

        {visibleError !== null && <p role="alert">{visibleError}</p>}
        <button disabled={submitting} type="submit">
          {submitting ? "Submitting…" : "Process documents"}
        </button>
        <p>
          Every completed document uses Gemini for its summary, including files extracted locally
          with PyPDF or parsed with Mistral OCR.
        </p>
      </form>

      <section aria-labelledby="results-heading">
        <h2 id="results-heading">Results</h2>
        <p aria-live="polite" role="status">
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
  return (
    <article aria-labelledby={`document-${document.id}`}>
      <header>
        <h2 id={`document-${document.id}`}>{document.filename}</h2>
        <p>{document.status}</p>
      </header>

      {document.status === "completed" && (
        <>
          <section aria-labelledby={`summary-${document.id}`}>
            <h3 id={`summary-${document.id}`}>Summary</h3>
            <ReactMarkdown>{document.summary}</ReactMarkdown>
          </section>
          <div>
            {document.pages.map((page) => (
              <details key={page.index}>
                <summary>Page {page.index + 1}</summary>
                <ReactMarkdown>{page.markdown}</ReactMarkdown>
              </details>
            ))}
          </div>
        </>
      )}

      {document.status === "failed" && (
        <div>
          <p role="alert">{document.error.message}</p>
          {document.error.code === "empty_extraction" && (
            <p>
              Try again with Scanned document / OCR / Mistral. The app will not switch parsers
              automatically.
            </p>
          )}
        </div>
      )}
    </article>
  );
}

function statusMessage(documents: readonly TrackedDocument[], pollError: string | null): string {
  if (pollError !== null) return pollError;
  if (documents.length === 0) return "No documents submitted yet.";
  const active = documents.filter(
    (document) => document.status === "queued" || document.status === "processing",
  );
  if (active.length === 0) return "All documents have finished processing.";
  if (active.length === 1) return `1 document ${active[0]?.status ?? "processing"}.`;
  return `${active.length} documents are still processing.`;
}
