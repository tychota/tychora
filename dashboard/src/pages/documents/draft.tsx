import {
  createContext,
  useEffect,
  useContext,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
  type ReactNode,
} from "react";
import type { JobHandle } from "../../api/operations";
import { SubmissionIntent } from "../sources/submission";
import type { ApiClient } from "../../api/client";
export interface AddDocumentDraft {
  type: string;
  key: string;
  kind: string;
  collection: string;
  format: string;
  text: string;
  path: string;
  bytes: string | null;
  file: File | null;
  metadata: string;
  fields: Record<string, string>;
  advanced: boolean;
  receipt: JobHandle | null;
  setupReturn: {
    route: "/dashboard/documents/add";
    step: "edit";
    draftId: string;
  } | null;
}
interface DraftContext {
  draft: AddDocumentDraft;
  setDraft: Dispatch<SetStateAction<AddDocumentDraft>>;
  draftId: string;
  intent: SubmissionIntent;
  /** Task 6 retains this exact client/scope for receipt-bound PDF requests. */
  receiptOwner: React.RefObject<ApiClient | null>;
}
const Context = createContext<DraftContext | null>(null);
export function AddDocumentDraftProvider({
  children,
}: {
  children: ReactNode;
}) {
  const [draft, setDraft] = useState<AddDocumentDraft>({
    type: "text",
    key: "",
    kind: "document@1",
    collection: "",
    format: "",
    text: "",
    path: "",
    bytes: null,
    file: null,
    metadata: "{}",
    fields: {},
    advanced: false,
    receipt: null,
    setupReturn: null,
  });
  const [draftId] = useState(() => crypto.randomUUID());
  const intent = useRef(new SubmissionIntent());
  const receiptOwner = useRef<ApiClient | null>(null);
  useEffect(() => () => receiptOwner.current?.dispose(), []);
  return (
    <Context.Provider
      value={{ draft, setDraft, draftId, intent: intent.current, receiptOwner }}
    >
      {children}
    </Context.Provider>
  );
}
export function useAddDocumentDraft() {
  const value = useContext(Context);
  if (!value) throw new Error("AddDocumentDraftProvider is required");
  return value;
}
