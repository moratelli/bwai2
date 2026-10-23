import { useAtomValue, useSetAtom, useStore } from "jotai";
import { useCallback, useEffect, useRef } from "react";

import type { AcceptedDocument, DocumentReader, TrackedDocument } from "./api";
import { documentsAtom, pollingErrorAtom, trackedDocumentIdsAtom } from "./pollingAtoms";

interface PollingOptions {
  intervalMs?: number;
  maxIntervalMs?: number;
}

interface PollingState {
  documents: TrackedDocument[];
  pollError: string | null;
  track: (documents: readonly AcceptedDocument[]) => void;
  clear: () => void;
}

const TERMINAL_STATUSES = new Set(["completed", "failed"]);

export function useDocumentPolling(
  reader: DocumentReader,
  options: PollingOptions = {},
): PollingState {
  const intervalMs = options.intervalMs ?? 1_000;
  const maxIntervalMs = options.maxIntervalMs ?? 8_000;
  const documents = useAtomValue(documentsAtom);
  const trackedIds = useAtomValue(trackedDocumentIdsAtom);
  const pollError = useAtomValue(pollingErrorAtom);
  const setDocuments = useSetAtom(documentsAtom);
  const setTrackedIds = useSetAtom(trackedDocumentIdsAtom);
  const setPollError = useSetAtom(pollingErrorAtom);
  const store = useStore();
  const readerRef = useRef(reader);
  readerRef.current = reader;

  const track = useCallback(
    (accepted: readonly AcceptedDocument[]) => {
      const current = store.get(documentsAtom);
      const byId = new Map(current.map((document) => [document.id, document]));
      for (const document of accepted) byId.set(document.id, document);
      setDocuments([...byId.values()]);
      setTrackedIds((tracked) => [...new Set([...tracked, ...accepted.map(({ id }) => id)])]);
      setPollError(null);
    },
    [setDocuments, setPollError, setTrackedIds, store],
  );

  const clear = useCallback(() => {
    setDocuments([]);
    setTrackedIds([]);
    setPollError(null);
  }, [setDocuments, setPollError, setTrackedIds]);

  useEffect(() => {
    if (trackedIds.length === 0) return;

    const controller = new AbortController();
    let active = true;
    let timer: number | undefined;
    let consecutiveFailures = 0;

    const poll = async () => {
      const current = store.get(documentsAtom);
      const activeIds = trackedIds.filter((id) => {
        const document = current.find((candidate) => candidate.id === id);
        return document !== undefined && !TERMINAL_STATUSES.has(document.status);
      });
      if (activeIds.length === 0) return;

      const results = await Promise.allSettled(
        activeIds.map((id) => readerRef.current.getDocument(id, controller.signal)),
      );
      if (!active) return;

      const updates = new Map<string, TrackedDocument>();
      let hadFailure = false;
      for (const result of results) {
        if (result.status === "fulfilled") updates.set(result.value.id, result.value);
        else hadFailure = true;
      }
      const next = store.get(documentsAtom).map((document) => updates.get(document.id) ?? document);
      setDocuments(next);

      if (hadFailure) {
        consecutiveFailures += 1;
        setPollError("Results are temporarily unavailable. Retrying…");
      } else {
        consecutiveFailures = 0;
        setPollError(null);
      }

      const hasActive = next.some((document) => !TERMINAL_STATUSES.has(document.status));
      if (hasActive) {
        const delay = Math.min(intervalMs * 2 ** consecutiveFailures, maxIntervalMs);
        timer = window.setTimeout(() => void poll(), delay);
      }
    };

    timer = window.setTimeout(() => void poll(), intervalMs);
    return () => {
      active = false;
      controller.abort();
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [intervalMs, maxIntervalMs, setDocuments, setPollError, store, trackedIds]);

  return { documents, pollError, track, clear };
}
