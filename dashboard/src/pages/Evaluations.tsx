import { useState } from "react";
import { useSession } from "../session";
import { useResource } from "../hooks/useResource";
import {
  objectPath,
  type Evaluation,
  type EvaluationSummary,
} from "../api/operations";
import { PageHeading, LoadState, RouteLink } from "../components/Operations";
import { JsonDetails } from "../components/JsonDetails";
import { Status } from "../components/Status";
import { EvaluationForm } from "./evaluations/EvaluationForm";
import { Metrics } from "./evaluations/Metrics";
import { QueryInspection } from "./evaluations/QueryInspection";
import { compareEvaluations } from "./evaluations/compare";
import { DatasetForm } from "./evaluations/DatasetForm";
export function Evaluations({ id }: { id?: string }) {
  const { handshake } = useSession();
  if (!handshake?.scope.capabilities.includes("manage"))
    return (
      <>
        <PageHeading title="Evaluations" />
        <p>
          Evaluation listing, inspection and submission require manage
          capability.
        </p>
      </>
    );
  return id ? <EvaluationDetail id={id} /> : <EvaluationList />;
}
function EvaluationList() {
  const { adminClient } = useSession();
  const data = useResource<EvaluationSummary[]>("/v1/evaluations", adminClient);
  const [selected, setSelected] = useState<string[]>([]);
  const [showForm, setShowForm] = useState(false);
  return (
    <>
      <PageHeading title="Evaluations">
        <p>Recorded retrieval runs, actual metrics and retained cases.</p>
      </PageHeading>
      <button type="button" onClick={() => setShowForm((v) => !v)}>
        Submit evaluation
      </button>
      {showForm && <EvaluationForm />}
      <DatasetForm />
      <LoadState {...data} retry={() => void data.load()} />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Compare</th>
              <th>Evaluation</th>
              <th>Dataset</th>
              <th>Created</th>
              <th>Availability</th>
            </tr>
          </thead>
          <tbody>
            {data.value?.map((run) => (
              <tr key={run.evaluation_id}>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Compare ${run.evaluation_id}`}
                    checked={selected.includes(run.evaluation_id)}
                    disabled={
                      !run.available ||
                      (!selected.includes(run.evaluation_id) &&
                        selected.length >= 2)
                    }
                    onChange={(e) =>
                      setSelected((old) =>
                        e.target.checked
                          ? [...old, run.evaluation_id]
                          : old.filter((id) => id !== run.evaluation_id),
                      )
                    }
                  />
                </td>
                <td>
                  <RouteLink
                    to={`/dashboard/evaluations/${objectPath(run.evaluation_id)}`}
                  >
                    {run.evaluation_id}
                  </RouteLink>
                </td>
                <td>{run.dataset_id}</td>
                <td>{run.created_at}</td>
                <td>{run.available ? "Available" : "Unavailable"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.value?.length === 0 && (
        <p>No authorized evaluations returned in the recent service window.</p>
      )}
      <p className="muted">
        Select two available runs to compare common numeric metrics. The service
        considers at most 100 most recent records before authorization
        filtering. Older evaluations may be omitted; continuation is
        unavailable.
      </p>
      {selected.length === 2 && (
        <Comparison
          key={selected.join(":")}
          leftId={selected[0]}
          rightId={selected[1]}
        />
      )}
    </>
  );
}
function EvaluationDetail({ id }: { id: string }) {
  const { adminClient } = useSession();
  const data = useResource<Evaluation>(
    `/v1/evaluations/${objectPath(id)}`,
    adminClient,
  );
  const run = data.value;
  return (
    <>
      <PageHeading title="Evaluation details">
        <RouteLink to="/dashboard/evaluations">All evaluations</RouteLink>
      </PageHeading>
      <LoadState {...data} retry={() => void data.load()} />
      {run && <EvaluationContent run={run} />}
    </>
  );
}
function EvaluationContent({ run }: { run: Evaluation }) {
  return (
    <section>
      <dl className="identity">
        <dt>Evaluation</dt>
        <dd>{run.evaluation_id}</dd>
        <dt>Dataset / split</dt>
        <dd>
          {run.dataset_id} / {run.report.split}
        </dd>
        <dt>Snapshot</dt>
        <dd>{run.snapshot_id}</dd>
        <dt>Configuration</dt>
        <dd>{run.config_sha}</dd>
        <dt>Policy epoch</dt>
        <dd>{run.policy_epoch}</dd>
        <dt>Execution context</dt>
        <dd>
          {run.hardware.offline === true
            ? "Offline"
            : run.hardware.offline === false
              ? "Online permitted"
              : "Unknown"}{" "}
          · {String(run.hardware.output_model ?? "Unknown model")}
        </dd>
      </dl>
      <JsonDetails
        value={run.hardware}
        label="Hardware, provider models and evaluation context"
      />
      <h2>Aggregate metrics</h2>
      <Metrics metrics={run.report.aggregate.metrics} />
      <h2>Case drilldown</h2>
      {Object.entries(run.cases).map(([id, value]) => (
        <details className="operation-panel" key={id}>
          <summary>
            {id} · {value.runs.length} runs
          </summary>
          {value.runs.map((caseRun, i) => (
            <article key={`${caseRun.run_id}-${i}`}>
              <h3>
                {caseRun.variant} · repetition {caseRun.repetition}
              </h3>
              <Metrics metrics={caseRun.metrics.metrics} />
              <JsonDetails value={caseRun.request} label="Case request" />
              <JsonDetails
                value={caseRun.execution_plan}
                label="Execution plan, when retained"
              />
              <QueryInspection result={caseRun.result} />
            </article>
          ))}
        </details>
      ))}
    </section>
  );
}
function Comparison({ leftId, rightId }: { leftId: string; rightId: string }) {
  const { adminClient } = useSession();
  const left = useResource<Evaluation>(
    `/v1/evaluations/${objectPath(leftId)}`,
    adminClient,
  );
  const right = useResource<Evaluation>(
    `/v1/evaluations/${objectPath(rightId)}`,
    adminClient,
  );
  const comparison =
    left.value && right.value
      ? compareEvaluations(left.value, right.value)
      : null;
  return (
    <section className="operation-panel">
      <h2>Compare evaluations</h2>
      <p>
        Left: {leftId} · Right: {rightId}. Deltas are right minus left.
      </p>
      <LoadState {...left} retry={() => void left.load()} />
      <LoadState {...right} retry={() => void right.load()} />
      {comparison && (
        <>
          {comparison.differences.length > 0 ? (
            <Status tone="warning">
              Identity or execution context differs. Arithmetic deltas do not
              establish comparable quality.
            </Status>
          ) : (
            <p>
              Recorded identity and context match. Unreported context remains
              unknown.
            </p>
          )}
          <JsonDetails
            value={comparison.differences}
            label="Identity and context differences"
          />
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Common numeric metric</th>
                  <th>Left</th>
                  <th>Right</th>
                  <th>Delta</th>
                </tr>
              </thead>
              <tbody>
                {comparison.deltas.map((m) => (
                  <tr key={m.name}>
                    <td>{m.name}</td>
                    <td>{m.left}</td>
                    <td>{m.right}</td>
                    <td>{m.delta}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {comparison.deltas.length === 0 && (
            <p>No common numeric metrics to subtract.</p>
          )}
          <details>
            <summary>Left case drilldown</summary>
            <EvaluationContent run={left.value!} />
          </details>
          <details>
            <summary>Right case drilldown</summary>
            <EvaluationContent run={right.value!} />
          </details>
        </>
      )}
    </section>
  );
}
