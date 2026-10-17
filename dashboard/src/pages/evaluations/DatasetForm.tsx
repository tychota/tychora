import { useEffect, useRef, useState } from "react";
import { useSession } from "../../session";
import { boundedBody } from "../../api/administration";
import { ErrorNotice, Status } from "../../components/Status";
export function DatasetForm() {
  const { adminClient } = useSession();
  const [manifest, setManifest] = useState("");
  const [cases, setCases] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<{
    dataset_id: string;
    version_sha: string;
  } | null>(null);
  const lifetime = useRef(new AbortController());
  useEffect(() => () => lifetime.current.abort(), []);
  return (
    <details className="operation-panel">
      <summary>Register evaluation dataset</summary>
      <p>
        Submit manifest JSON and cases JSONL as bounded UTF-8 data, under the
        ordinary 1 MiB request limit. Labels are validated by the server.
      </p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          if (!adminClient || busy) return;
          setBusy(true);
          setError(null);
          setResult(null);
          try {
            const body = boundedBody({
              manifest_json: manifest,
              cases_jsonl: cases,
            });
            const value = await adminClient.request<{
              dataset_id: string;
              version_sha: string;
            }>("/v1/evaluations/datasets", {
              method: "POST",
              body,
              signal: lifetime.current.signal,
            });
            if (!lifetime.current.signal.aborted) {
              setResult(value);
              setManifest("");
              setCases("");
            }
          } catch (e) {
            if (!lifetime.current.signal.aborted) setError(e);
          } finally {
            if (!lifetime.current.signal.aborted) setBusy(false);
          }
        }}
      >
        <fieldset disabled={busy}>
          <label>
            Dataset manifest JSON
            <textarea
              rows={5}
              required
              value={manifest}
              onChange={(e) => setManifest(e.target.value)}
            />
          </label>
          <label>
            Dataset cases JSONL
            <textarea
              rows={7}
              required
              value={cases}
              onChange={(e) => setCases(e.target.value)}
            />
          </label>
          <button>Register dataset</button>
        </fieldset>
      </form>
      {error !== null && <ErrorNotice error={error} />}
      {result && (
        <Status tone="success">
          Registered dataset {result.dataset_id} · version {result.version_sha}.
          Use this dataset ID when submitting an evaluation.
        </Status>
      )}
    </details>
  );
}
