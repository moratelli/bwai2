import { atom } from "jotai";

import type { TrackedDocument } from "./api";

export const documentsAtom = atom<TrackedDocument[]>([]);
export const trackedDocumentIdsAtom = atom<string[]>([]);
export const pollingErrorAtom = atom<string | null>(null);
