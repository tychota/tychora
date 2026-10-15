import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSession } from "../../session";
import { ApiError } from "../../api/client";
import type { JobHandle } from "../../api/operations";
import { encodedBody, REQUEST_LIMIT } from "../sources/submission";
import { ErrorNotice, Status } from "../../components/Status";
import { RouteLink } from "../../components/Operations";
export function EvaluationForm() {
  const { adminClient: client } = useSession();
  const [dataset, setDataset] = useState("");
  const [split, setSplit] = useState("test");
  const [snapshot, setSnapshot] = useState("");
  const [variants, setVariants] = useState(
    '[{"name":"baseline","profile":"local","overrides":{}}]',
  );
  const [seed, setSeed] = useState(0);
  const [repetitions, setRepetitions] = useState(1);
  const [offline, setOffline] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [receipt, setReceipt] = useState<JobHandle | null>(null);
  const lifetime = useRef(new AbortController());
  useEffect(() => () => lifetime.current.abort(), []);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!client || busy) return;
    setError(null);
    setReceipt(null);
    let body: string;
    try {
      let value: unknown;
      try {
        value = JSON.parse(variants);
      } catch {
        throw new ApiError("INVALID_REQUEST", "Variants must be a JSON array.");
      }
      if (!Array.isArray(value) || !value.length)
        throw new ApiError(
          "INVALID_REQUEST",
          "Supply at least one configuration variant.",
        );
      const encoded = encodedBody({
        dataset_id: dataset,
        split,
        snapshot_id: snapshot,
        variants: value,
        seed,
        repetitions,
        offline,
      });
      if (encoded.size >= REQUEST_LIMIT)
        throw new ApiError(
          "INPUT_BUDGET_EXCEEDED",
          "Evaluation request exceeds the 1 MiB limit.",
        );
      body = encoded.body;
    } catch (error) {
      setError(error);
      return;
    }
    setBusy(true);
    try {
      const value = await client.request<JobHandle>("/v1/evaluations", {
        method: "POST",
        body,
        signal: lifetime.current.signal,
      });
      if (!lifetime.current.signal.aborted) setReceipt(value);
    } catch (error) {
      if (!lifetime.current.signal.aborted) setError(error);
    } finally {
      if (!lifetime.current.signal.aborted) setBusy(false);
    }
  };
  return (
    <section className="operation-panel">
      <h2>Submit evaluation</h2>
      <p>
        Use an already registered dataset ID and an existing snapshot. Dataset
        registration is available on the evaluation list. Label editing is
        unavailable.
      </p>
      <form onSubmit={(e) => void submit(e)}>
        <fieldset disabled={busy}>
          <div className="field-grid">
            <label>
              Registered dataset ID
              <input
                value={dataset}
                onChange={(e) => setDataset(e.target.value)}
                required
              />
            </label>
            <label>
              Dataset split
              <input
                value={split}
                onChange={(e) => setSplit(e.target.value)}
                required
              />
            </label>
            <label>
              Snapshot ID
              <input
                value={snapshot}
                onChange={(e) => setSnapshot(e.target.value)}
                required
              />
            </label>
            <label>
              Seed
              <input
                type="number"
                min={0}
                step={1}
                value={seed}
                onChange={(e) => setSeed(Number(e.target.value))}
                required
              />
            </label>
            <label>
              Repetitions
              <input
                type="number"
                min={1}
                max={100}
                step={1}
                value={repetitions}
                onChange={(e) => setRepetitions(Number(e.target.value))}
                required
              />
            </label>
          </div>
          <label>
            Configuration variants JSON
            <textarea
              value={variants}
              onChange={(e) => setVariants(e.target.value)}
              rows={5}
              required
              spellCheck={false}
            />
          </label>
          <p className="muted">
            Each variant requires name and registered profile; overrides is an
            optional object.
          </p>
          <label className="collection-option">
            <input
              type="checkbox"
              checked={offline}
              onChange={(e) => setOffline(e.target.checked)}
            />
            Offline evaluation
          </label>
          <p>
            The request remains open until evaluation completes. Provider
            configuration and server policy govern execution.
          </p>
          <button type="submit" className="primary">
            {busy ? "Evaluation running; request in flight…" : "Run evaluation"}
          </button>
        </fieldset>
      </form>
      {busy && (
        <Status>
          Waiting for the evaluation response. This is an active request.
        </Status>
      )}
      {error !== null && <ErrorNotice error={error} />}{" "}
      {receipt && (
        <Status tone="success">
          Evaluation response · {receipt.state} ·{" "}
          <RouteLink
            to={`/dashboard/evaluations/${encodeURIComponent(receipt.id)}`}
          >
            Inspect evaluation
          </RouteLink>
        </Status>
      )}
    </section>
  );
}
