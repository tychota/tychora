import { useEffect, useRef, useState } from "react";
import { usePages } from "../hooks/useResource";
import type { DocumentPage, DocumentSummary } from "../api/contracts";
import type { JobHandle, ExtractionPage } from "../api/operations";
import { objectPath } from "../api/operations";
import { useSession } from "../session";
import { useRoute } from "../routing";
import {
  PageHeading,
  LoadState,
  More,
  RouteLink,
} from "../components/Operations";
import { ErrorNotice, Status } from "../components/Status";
import { JsonDetails } from "../components/JsonDetails";
import { OriginalInspection } from "../components/EvidenceReader";
import { SourceRevisions, RevisionCoverage } from "./sources/Coverage";
import { SourceContent } from "./documents/SourceContent";
import { SubmissionIntent } from "./sources/submission";
const tabs = [
  "Overview",
  "Content",
  "Chunks",
  "Relationships",
  "History",
] as const;
export function Documents({
  sourceId,
  revisionId,
}: {
  sourceId?: string;
  revisionId?: string;
}) {
  const data = usePages<DocumentPage>(revisionId ? null : "/v1/documents");
  const { handshake, adminClient } = useSession();
  const [filter, setFilter] = useState("");
  const { path, navigate } = useRoute();
  const rows = [
    ...new Map(
      data.pages.flatMap((p) => p.documents).map((d) => [d.source_id, d]),
    ).values(),
  ];
  const selected = rows.find((d) => d.source_id === sourceId);
  const heading = useRef<HTMLHeadingElement>(null);
  const lastOpened = useRef<string | null>(null);
  useEffect(() => {
    if (sourceId) {
      lastOpened.current = sourceId;
      heading.current?.focus();
    } else if (lastOpened.current) {
      document.getElementById(`document-${lastOpened.current}`)?.focus();
    }
  }, [sourceId, selected]);
  return (
    <>
      <PageHeading title="Documents">
        <div className="heading-actions">
          <p>
            Read indexed content and retained provenance in the selected
            collections.
          </p>
          {handshake?.scope.capabilities.includes("write") ? (
            <RouteLink className="button primary" to="/dashboard/documents/add">
              Add documents
            </RouteLink>
          ) : (
            <p className="muted">
              Adding and syncing sources requires write capability.
            </p>
          )}
        </div>
      </PageHeading>
      <div className={`document-workspace ${sourceId ? "has-detail" : ""}`}>
        <section className="document-inventory" aria-label="Document inventory">
          <div className="inventory-toolbar">
            <label>
              Find in loaded documents
              <input
                type="search"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            </label>
            <button onClick={() => void data.load()}>Refresh documents</button>
          </div>
          <p className="muted">
            {rows.length} loaded{data.next ? " · More available" : ""}.
            Filtering applies to loaded rows.
          </p>
          <LoadState {...data} retry={() => void data.load()} />
          <ol className="document-list">
            {rows
              .filter((d) =>
                `${d.title} ${d.source_key} ${d.collection_title}`
                  .toLowerCase()
                  .includes(filter.toLowerCase()),
              )
              .map((d) => (
                <li key={d.source_id}>
                  <button
                    id={`document-${d.source_id}`}
                    className="document-row"
                    aria-pressed={sourceId === d.source_id}
                    onClick={() =>
                      navigate(
                        `/dashboard/documents/${objectPath(d.source_id)}`,
                      )
                    }
                  >
                    <strong>{d.title || d.source_key}</strong>
                    <span>
                      {d.collection_title} · {d.source_type}
                    </span>
                    <span className="muted">
                      Processing: {d.processing_state}
                      {d.published_content_state === "published"
                        ? " · Published content observed"
                        : " · Publication not observed"}
                    </span>
                  </button>
                </li>
              ))}
          </ol>
          {!data.busy && data.pages.length > 0 && !rows.length && (
            <section className="empty-state">
              <h2>Add a document to start reading</h2>
              <p>
                No documents were returned in this scope. Choose Add documents
                to index text into one named collection.
              </p>
            </section>
          )}
          <More
            {...data}
            onClick={() => void data.more()}
            label="Load more documents"
          />
        </section>
        <section
          className="document-detail reader-surface"
          aria-label="Selected document"
        >
          {sourceId ? (
            <>
              <div className="detail-toolbar">
                <button
                  onClick={() => navigate("/dashboard/documents")}
                  className="back-to-documents"
                >
                  Back to documents
                </button>
              </div>
              <h2 tabIndex={-1} ref={heading}>
                {selected?.title ||
                  selected?.source_key ||
                  "Document inspection"}
              </h2>
              {selected ? (
                <DocumentDetail
                  key={`${selected.source_id}-${revisionId ?? "reported"}`}
                  document={selected}
                  revisionId={revisionId}
                />
              ) : revisionId ? (
                <>
                  <p>
                    Explicit retained revision. Inventory availability does not
                    establish current processing state.
                  </p>
                  <RevisionCoverage
                    sourceId={sourceId}
                    revisionId={revisionId}
                    client={adminClient}
                  />
                  <SourceRevisions sourceId={sourceId} client={adminClient} />
                </>
              ) : (
                <p>
                  {data.busy
                    ? "Loading document inventory…"
                    : data.next
                      ? "This identity has not been returned in the loaded inventory. Load more documents to continue."
                      : "This document is unavailable in the selected scope."}
                </p>
              )}
            </>
          ) : (
            <div className="reader-empty">
              <p className="eyebrow">Document workspace</p>
              <h2>Select a document</h2>
              <p>
                Read extracted content, exact stored chunks, relationships and
                retained history.
              </p>
            </div>
          )}
        </section>
      </div>
      {path.startsWith("/dashboard/sources") && (
        <p className="muted">
          This saved source link opens the Documents workspace.
        </p>
      )}
    </>
  );
}
function DocumentDetail({
  document: d,
  revisionId,
}: {
  document: DocumentSummary;
  revisionId?: string;
}) {
  const session = useSession();
  const [tab, setTab] = useState<(typeof tabs)[number]>(
    revisionId ? "History" : "Overview",
  );
  const revision = revisionId ?? d.revision_id;
  const extractions = usePages<ExtractionPage>(
    revision && ["Content", "Chunks", "Relationships"].includes(tab)
      ? `/v1/sources/${objectPath(d.source_id)}/revisions/${objectPath(revision)}/extractions`
      : null,
  );
  const [extraction, setExtraction] = useState(
    revisionId ? "" : (d.extraction_id ?? ""),
  );
  const [confirmWithdraw, setConfirmWithdraw] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [receipt, setReceipt] = useState<unknown>(null);
  const [syncReceipt, setSyncReceipt] = useState<JobHandle | null>(null);
  const intent = useRef(new SubmissionIntent());
  const lifetime = useRef(new AbortController());
  useEffect(() => () => lifetime.current.abort(), []);
  const action = async (withdraw: boolean) => {
    if (!session.client || busy) return;
    setBusy(true);
    setError(null);
    const body = JSON.stringify({
      operation: "remove",
      source_ids: [d.source_id],
    });
    try {
      const result = await session.client.request(
        withdraw ? "/v1/jobs" : `/v1/sources/${objectPath(d.source_id)}/sync`,
        {
          method: "POST",
          ...(withdraw ? { body } : {}),
          headers: {
            "Idempotency-Key": intent.current.keyFor(
              withdraw ? body : `sync:${d.source_id}`,
            ),
          },
          signal: lifetime.current.signal,
        },
      );
      if (!lifetime.current.signal.aborted) {
        if (withdraw) setReceipt(result);
        else setSyncReceipt(result as JobHandle);
        setConfirmWithdraw(false);
      }
    } catch (e) {
      if (!lifetime.current.signal.aborted) setError(e);
    } finally {
      if (!lifetime.current.signal.aborted) setBusy(false);
    }
  };
  const available = extractions.pages.flatMap((p) => p.extractions);
  const lineage =
    revision && extraction
      ? {
          sourceId: d.source_id,
          revisionId: revision,
          extractionId: extraction,
        }
      : null;
  return (
    <>
      <p className="muted">
        {d.collection_title} · Processing: {d.processing_state}.{" "}
        {d.published_content_state === "published"
          ? "Published content observed."
          : "Publication not observed."}
      </p>
      <div role="tablist" aria-label="Document views" className="document-tabs">
        {tabs.map((name, index) => (
          <button
            role="tab"
            id={`tab-${name}`}
            aria-selected={tab === name}
            aria-controls="document-tab-panel"
            tabIndex={tab === name ? 0 : -1}
            key={name}
            onClick={() => setTab(name)}
            onKeyDown={(e) => {
              const next =
                e.key === "ArrowRight"
                  ? (index + 1) % tabs.length
                  : e.key === "ArrowLeft"
                    ? (index + tabs.length - 1) % tabs.length
                    : e.key === "Home"
                      ? 0
                      : e.key === "End"
                        ? tabs.length - 1
                        : null;
              if (next !== null) {
                e.preventDefault();
                setTab(tabs[next]);
                document.getElementById(`tab-${tabs[next]}`)?.focus();
              }
            }}
          >
            {name}
          </button>
        ))}
      </div>
      <section
        id="document-tab-panel"
        role="tabpanel"
        aria-labelledby={`tab-${tab}`}
      >
        {tab === "Overview" ? (
          <>
            <dl className="identity">
              <dt>Source type</dt>
              <dd>{d.source_type}</dd>
              <dt>Original type</dt>
              <dd>{d.original_media_type ?? "Unknown"}</dd>
              <dt>Original size</dt>
              <dd>
                {d.original_size_bytes === null
                  ? "Unknown"
                  : `${d.original_size_bytes.toLocaleString()} bytes`}
              </dd>
              <dt>Created</dt>
              <dd>{d.created_at ?? "Unknown"}</dd>
              <dt>Captured</dt>
              <dd>{d.captured_at ?? "Unknown"}</dd>
              <dt>Units / chunks</dt>
              <dd>
                {d.unit_count ?? "Unknown"} / {d.window_count ?? "Unknown"}
              </dd>
              <dt>Observed channels</dt>
              <dd>
                {d.channel_observations
                  .map((c) => `${c.channel}: ${c.state}`)
                  .join(", ") || "None reported"}
                {!d.channel_observations_complete && " · partial observations"}
              </dd>
            </dl>
            <p className="muted">
              Retained lineage and channel observations do not establish current
              job state or active snapshot membership.
            </p>
            {revision && (
              <>
                <RouteLink
                  to={`/dashboard/documents/${objectPath(d.source_id)}/revisions/${objectPath(revision)}`}
                >
                  Open revision
                </RouteLink>
                {session.client && (
                  <OriginalInspection
                    client={session.client}
                    revisionId={revision}
                  />
                )}
              </>
            )}
            <details>
              <summary>Processing and lifecycle</summary>
              {session.handshake?.scope.capabilities.includes("write") && (
                <button disabled={busy} onClick={() => void action(false)}>
                  Sync source
                </button>
              )}
              {session.handshake?.scope.capabilities.includes("manage") && (
                <button
                  disabled={busy}
                  onClick={() => setConfirmWithdraw(true)}
                >
                  Withdraw source
                </button>
              )}
              {confirmWithdraw && (
                <div>
                  <p>
                    Withdrawal excludes this source from ordinary search and
                    further synchronization. Retained evidence and original
                    bytes follow the workspace retention policy; this does not
                    physically purge them.
                  </p>
                  <button disabled={busy} onClick={() => void action(true)}>
                    Confirm withdrawal
                  </button>
                  <button onClick={() => setConfirmWithdraw(false)}>
                    Keep source
                  </button>
                </div>
              )}
            </details>
            <JsonDetails
              value={d}
              label="Document identity and reported observations"
            />
          </>
        ) : tab === "History" ? (
          <>
            <SourceRevisions sourceId={d.source_id} />
            {revisionId && (
              <RevisionCoverage
                sourceId={d.source_id}
                revisionId={revisionId}
              />
            )}
          </>
        ) : (
          <>
            <label>
              Retained extraction
              <select
                value={extraction}
                onChange={(e) => setExtraction(e.target.value)}
              >
                <option value="">Choose an extraction</option>
                {extraction &&
                  !available.some((e) => e.extraction_id === extraction) && (
                    <option value={extraction}>
                      Reported extraction · {extraction.slice(0, 8)}
                    </option>
                  )}
                {available.map((e, i) => (
                  <option key={e.extraction_id} value={e.extraction_id}>
                    Extraction {i + 1} · {e.extraction_id.slice(0, 8)}
                  </option>
                ))}
              </select>
            </label>
            <LoadState {...extractions} retry={() => void extractions.load()} />
            <More
              {...extractions}
              onClick={() => void extractions.more()}
              label="Load more extractions"
            />
            {lineage && (
              <JsonDetails
                value={lineage}
                label="Retained extraction identity"
              />
            )}
            {lineage ? (
              <SourceContent
                key={`${extraction}-${tab}`}
                lineage={lineage}
                view={
                  tab === "Content"
                    ? "content"
                    : tab === "Chunks"
                      ? "chunks"
                      : "relationships"
                }
              />
            ) : (
              <p>
                Choose a retained extraction to inspect its content. No
                extraction is selected automatically for an explicit revision.
              </p>
            )}
          </>
        )}
      </section>
      {error !== null && <ErrorNotice error={error} />}
      {receipt !== null && (
        <>
          <Status>
            Source withdrawal accepted. The server receipt describes retention
            and cleanup.
          </Status>
          <JsonDetails value={receipt} label="Withdrawal receipt" />
        </>
      )}
      {syncReceipt && (
        <Status>
          Sync accepted ·{" "}
          <RouteLink to={`/dashboard/jobs/${objectPath(syncReceipt.id)}`}>
            Follow job
          </RouteLink>
        </Status>
      )}
    </>
  );
}
