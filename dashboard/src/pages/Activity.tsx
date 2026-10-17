import { useEffect, useRef, useState } from "react";
import { useSession } from "../session";
import { usePages, useResource } from "../hooks/useResource";
import type { Discovery, Inspection, QueryResult } from "../api/contracts";
import { objectPath } from "../api/operations";
import { ErrorNotice } from "../components/Status";
import {
  PageHeading,
  LoadState,
  More,
  RouteLink,
} from "../components/Operations";
import { JsonDetails, downloadJson } from "../components/JsonDetails";
import { QueryInspection } from "./evaluations/QueryInspection";
export function Activity({ id }: { id?: string }) {
  return id ? <RunDetail id={id} /> : <RunList />;
}
function RunList() {
  const data = usePages<Discovery>("/v1/queries");
  return (
    <>
      <PageHeading title="Activity">
        <p>
          Retrieval runs within the selected scope. Source operations are listed
          in <RouteLink to="/dashboard/jobs">Jobs</RouteLink>.
        </p>
      </PageHeading>
      <LoadState {...data} retry={() => void data.load()} />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Created</th>
              <th>State</th>
              <th>Retention</th>
            </tr>
          </thead>
          <tbody>
            {data.pages
              .flatMap((p) => p.definitions)
              .map((run, i) => (
                <tr key={`${run.id}-${i}`}>
                  <td>
                    <RouteLink to={`/dashboard/runs/${objectPath(run.id)}`}>
                      {run.id}
                    </RouteLink>
                  </td>
                  <td>{String(run.definition.created_at ?? "Unknown")}</td>
                  <td>{String(run.definition.state ?? "Unknown")}</td>
                  <td>
                    {run.definition.request_retained === true
                      ? "Request retained"
                      : "Query text not retained"}
                    {run.definition.scope_limited === true && (
                      <span className="muted">Scope limited</span>
                    )}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      <More {...data} onClick={() => void data.more()} label="Load more runs" />
      {!data.busy &&
        data.pages.length > 0 &&
        data.pages.every((p) => !p.definitions.length) && (
          <p>No retained run metadata in this scope.</p>
        )}
      <p className="muted">
        Only runs with this exact selected query scope are listed. Other scopes
        may retain history.
      </p>
    </>
  );
}
function RunDetail({ id }: { id: string }) {
  const { client } = useSession();
  const data = useResource<Inspection>(`/v1/queries/${objectPath(id)}`);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const lifetime = useRef(new AbortController());
  useEffect(() => () => lifetime.current.abort(), []);
  const summary = data.value?.records.find(
    (r) => r.record_type === "run_summary",
  )?.value;
  const retained = summary?.request_retained === true;
  const replay = async () => {
    if (!client || busy) return;
    setBusy(true);
    setError(null);
    try {
      const value = await client.request<QueryResult>(
        `/v1/queries/${objectPath(id)}/replay`,
        { method: "POST", signal: lifetime.current.signal },
      );
      if (!lifetime.current.signal.aborted) setResult(value);
    } catch (e) {
      if (!lifetime.current.signal.aborted) setError(e);
    } finally {
      if (!lifetime.current.signal.aborted) setBusy(false);
    }
  };
  return (
    <>
      <PageHeading title="Run details">
        <RouteLink to="/dashboard/activity">All query runs</RouteLink>
      </PageHeading>
      <LoadState {...data} retry={() => void data.load()} />
      {data.value && (
        <>
          <p className="object-id">{id}</p>
          <p>
            {retained
              ? "This request was retained. Replay explicitly reruns it against its retained dependencies."
              : "Query text not retained"}
          </p>
          <JsonDetails
            value={data.value}
            label="Retained run metadata and records"
          />
          {retained && (
            <button type="button" disabled={busy} onClick={() => void replay()}>
              {busy ? "Replaying…" : "Replay retained query"}
            </button>
          )}
          <p className="muted">
            Trace details are unavailable unless a retained result supplies
            them.
          </p>
        </>
      )}
      {error !== null && (
        <ErrorNotice error={error} retry={() => void replay()} />
      )}{" "}
      {result && (
        <>
          <button
            type="button"
            onClick={() => downloadJson(result, `replay-${result.run_id}.json`)}
          >
            Export replay JSON
          </button>
          <QueryInspection result={result} />
        </>
      )}
    </>
  );
}
