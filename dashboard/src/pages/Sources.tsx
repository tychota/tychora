import { useEffect, useRef, useState } from "react";
import { usePages } from "../hooks/useResource";
import type { Discovery } from "../api/contracts";
import type { JobHandle } from "../api/operations";
import { objectPath } from "../api/operations";
import { useSession } from "../session";
import {
  PageHeading,
  LoadState,
  More,
  RouteLink,
} from "../components/Operations";
import { ErrorNotice, Status } from "../components/Status";
import { JsonDetails } from "../components/JsonDetails";
import { SourceRevisions, RevisionCoverage } from "./sources/Coverage";
import { SourceForm } from "./sources/SourceForm";
export function Sources({
  sourceId,
  revisionId,
}: {
  sourceId?: string;
  revisionId?: string;
}) {
  const data = usePages<Discovery>("/v1/sources");
  const { client, handshake } = useSession();
  const [add, setAdd] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<unknown>(null);
  const [syncReceipt, setSyncReceipt] = useState<JobHandle | null>(null);
  const [syncing, setSyncing] = useState<string | null>(null);
  const syncKeys = useRef(new Map<string, string>());
  const lifetime = useRef(new AbortController());
  useEffect(() => () => lifetime.current.abort(), []);
  const canWrite = handshake?.scope.capabilities.includes("write");
  const sync = async (id: string) => {
    if (!client || syncing) return;
    let key = syncKeys.current.get(id);
    if (!key) {
      key = crypto.randomUUID();
      syncKeys.current.set(id, key);
    }
    setSyncing(id);
    setSyncError(null);
    try {
      const receipt = await client.request<JobHandle>(
        `/v1/sources/${objectPath(id)}/sync`,
        {
          method: "POST",
          headers: { "Idempotency-Key": key },
          signal: lifetime.current.signal,
        },
      );
      if (lifetime.current.signal.aborted) return;
      setSyncReceipt(receipt);
      syncKeys.current.delete(id);
    } catch (e) {
      if (!lifetime.current.signal.aborted) setSyncError(e);
    } finally {
      if (!lifetime.current.signal.aborted) setSyncing(null);
    }
  };
  if (sourceId && revisionId)
    return (
      <>
        <PageHeading title="Source revision" />
        <RevisionCoverage sourceId={sourceId} revisionId={revisionId} />
      </>
    );
  return (
    <>
      <PageHeading title="Sources">
        <p>
          Registered sources and retained revision coverage within the selected
          collections.
        </p>
      </PageHeading>
      {canWrite ? (
        <button type="button" onClick={() => setAdd((v) => !v)}>
          Add source
        </button>
      ) : (
        <p className="muted">
          Adding and syncing sources requires write capability.
        </p>
      )}
      {add && <SourceForm />}
      <LoadState {...data} retry={() => void data.load()} />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Source key</th>
              <th>Collection</th>
              <th>Type</th>
              <th>Inspection</th>
            </tr>
          </thead>
          <tbody>
            {data.pages
              .flatMap((p) => p.definitions)
              .map((source, i) => (
                <tr key={`${source.id}-${i}`}>
                  <td>
                    <strong>{source.label || source.id}</strong>
                    <span className="object-id">{source.id}</span>
                  </td>
                  <td>
                    {String(source.definition.collection_id ?? "Unknown")}
                  </td>
                  <td>{String(source.definition.source_type ?? "Unknown")}</td>
                  <td>
                    <button
                      type="button"
                      onClick={() => setSelected(source.id)}
                    >
                      Inspect revisions
                    </button>
                    {canWrite && (
                      <button
                        type="button"
                        disabled={syncing !== null}
                        onClick={() => void sync(source.id)}
                      >
                        Sync source
                      </button>
                    )}
                    <JsonDetails
                      value={source.definition}
                      label="Registration and coverage summary"
                    />
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      {!data.busy &&
        data.pages.length > 0 &&
        data.pages.every((p) => !p.definitions.length) && (
          <p>No registered sources in this scope.</p>
        )}
      <More
        {...data}
        onClick={() => void data.more()}
        label="Load more sources"
      />
      {syncError !== null && <ErrorNotice error={syncError} />}{" "}
      {syncReceipt && (
        <Status>
          Sync accepted ·{" "}
          <RouteLink to={`/dashboard/jobs/${objectPath(syncReceipt.id)}`}>
            Follow job
          </RouteLink>
        </Status>
      )}
      {selected && <SourceRevisions key={selected} sourceId={selected} />}
    </>
  );
}
