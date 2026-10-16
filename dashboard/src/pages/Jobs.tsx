import { useEffect, useRef, useState } from "react";
import { useSession } from "../session";
import { usePages, useResource } from "../hooks/useResource";
import {
  objectPath,
  terminal,
  type JobPage,
  type JobStatus,
  type JobEvent,
} from "../api/operations";
import { watchJob } from "../api/events";
import type { JobHandle } from "../api/types";
import { ErrorNotice, Status } from "../components/Status";
import {
  PageHeading,
  LoadState,
  More,
  RouteLink,
} from "../components/Operations";
import { JsonDetails } from "../components/JsonDetails";
function JobDetail({ id }: { id: string }) {
  const { adminClient: client, handshake } = useSession();
  const data = useResource<JobStatus>(`/v1/jobs/${objectPath(id)}`, client);
  const [after, setAfter] = useState(0);
  const retained = useResource<JobEvent[]>(
    `/v1/jobs/${objectPath(id)}/events/page?after=${after}`,
    client,
  );
  const [events, setEvents] = useState<JobEvent[]>([]);
  useEffect(() => {
    if (Array.isArray(retained.value))
      setEvents((old) =>
        Array.from(
          new Map(
            [...retained.value!, ...old].map((event) => [
              event.sequence,
              event,
            ]),
          ).values(),
        )
          .sort((a, b) => a.sequence - b.sequence)
          .slice(-100),
      );
  }, [retained.value]);
  const [streamError, setStreamError] = useState<unknown>(null);
  const [streamEpoch, setStreamEpoch] = useState(0);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<unknown>(null);
  const [retryReceipt, setRetryReceipt] = useState<JobHandle | null>(null);
  const initial = useRef<JobStatus | null>(null);
  const lifetime = useRef(new AbortController());
  useEffect(() => () => lifetime.current.abort(), []);
  useEffect(() => {
    if (!client || !data.value || initial.current) return;
    initial.current = data.value;
    setStreamEpoch((v) => v + 1);
  }, [client, data.value]);
  useEffect(() => {
    if (!client || !initial.current || terminal(initial.current.state)) return;
    const controller = new AbortController();
    setStreamError(null);
    const refresh = async () => {
      const value = await client.request<JobStatus>(
        `/v1/jobs/${objectPath(id)}`,
        { signal: controller.signal },
      );
      if (!controller.signal.aborted) {
        data.setValue(value);
        initial.current = value;
      }
      return value;
    };
    void watchJob(client, id, controller.signal, {
      after: initial.current.latest_sequence,
      refresh,
      onEvent: async (event) => {
        if (controller.signal.aborted) return;
        setEvents((old) =>
          old.some((item) => item.sequence === event.sequence)
            ? old
            : [...old, event].slice(-100),
        );
        await refresh();
      },
    }).catch((error) => {
      if (!controller.signal.aborted) setStreamError(error);
    });
    return () => controller.abort();
  }, [client, id, streamEpoch]);
  const action = async (name: "retry" | "cancel") => {
    if (!client || actionBusy) return;
    setActionBusy(true);
    setActionError(null);
    try {
      const result = await client.request<JobStatus | JobHandle>(
        `/v1/jobs/${objectPath(id)}/${name}`,
        { method: "POST", signal: lifetime.current.signal },
      );
      if (lifetime.current.signal.aborted) return;
      if (name === "retry") {
        setRetryReceipt(result);
        initial.current = null;
        await data.load();
      } else {
        data.setValue(result as JobStatus);
        initial.current = result as JobStatus;
      }
      setStreamEpoch((v) => v + 1);
    } catch (error) {
      if (!lifetime.current.signal.aborted) setActionError(error);
    } finally {
      if (!lifetime.current.signal.aborted) setActionBusy(false);
    }
  };
  const job = data.value;
  const write = handshake?.scope.capabilities.includes("write");
  const manage = handshake?.scope.capabilities.includes("manage");
  const sourceJob = job && ["add", "sync"].includes(job.operation);
  const mayRecover = sourceJob ? write : manage;
  const canResume =
    sourceJob &&
    write &&
    job.captured_inputs !== null &&
    !job.cancellation_requested &&
    (job.state === "queued" ||
      (job.state === "running" && job.retry_count < 3));
  return (
    <>
      <PageHeading title="Job details">
        <RouteLink to="/dashboard/jobs">All jobs</RouteLink>
      </PageHeading>
      <LoadState {...data} retry={() => void data.load()} />
      {job && (
        <>
          <dl className="identity">
            <dt>Job</dt>
            <dd>{job.id}</dd>
            <dt>Operation</dt>
            <dd>{job.operation}</dd>
            <dt>State</dt>
            <dd>
              {job.state}
              {job.cancellation_requested ? " · cancellation requested" : ""}
            </dd>
            <dt>Sequence</dt>
            <dd>{job.latest_sequence}</dd>
            <dt>Retry count</dt>
            <dd>{job.retry_count}</dd>
            <dt>Result snapshot</dt>
            <dd>{job.result_snapshot_id ?? "Unavailable"}</dd>
            <dt>Error code</dt>
            <dd>{job.error_code ?? "None reported"}</dd>
          </dl>
          {job.retention_limited && (
            <Status tone="warning">
              Event history is retention limited. Persisted status remains
              authoritative.
            </Status>
          )}
          <p>
            Source lineage is unavailable in this durable status.{" "}
            <RouteLink to="/dashboard/documents">Find the document</RouteLink>{" "}
            using its source identity; accepted document actions link to their
            actual jobs.
          </p>
          {!sourceJob && (
            <p>
              Retry and recovery are unavailable for this operation. The source
              scheduler only executes add/sync recovery.
            </p>
          )}
          <div className="unit-actions">
            {canResume && (
              <button
                disabled={actionBusy}
                onClick={() => void action("retry")}
              >
                Resume or recover job
              </button>
            )}
            {sourceJob &&
              write &&
              job.captured_inputs !== null &&
              job.retry_count < 3 &&
              ["failed", "interrupted"].includes(job.state) && (
                <button
                  disabled={actionBusy}
                  onClick={() => void action("retry")}
                >
                  Retry job
                </button>
              )}
            {mayRecover &&
              !terminal(job.state) &&
              !job.cancellation_requested && (
                <button
                  disabled={actionBusy}
                  onClick={() => void action("cancel")}
                >
                  Request cancellation
                </button>
              )}
            <button type="button" onClick={() => void data.load()}>
              Refresh status
            </button>
          </div>
          <p className="muted">
            Resume or recover asks the scheduler to keep an active worker or
            restart a retained queued/orphaned job. Queued resumption does not
            spend a retry; recovering an orphaned running job requires remaining
            retry budget. Failed retries require a transient failure. The server
            verifies eligibility.
          </p>
          <p className="muted">
            Disconnecting leaves the job running. Cancellation is a separate
            server action.
          </p>
          <JsonDetails value={job} label="Persisted job diagnostics" />
          <h2>Live events</h2>
          <p>
            At most 100 retained/received events are displayed. Persisted job
            status is authoritative; counts are reported, not estimated.
          </p>
          <LoadState {...retained} retry={() => void retained.load()} />
          {Array.isArray(retained.value) && retained.value.length === 100 && (
            <button onClick={() => setAfter(retained.value!.at(-1)!.sequence)}>
              Load later retained events
            </button>
          )}
          <ol className="compact-list">
            {events.map((event) => (
              <li key={event.sequence}>
                <strong>
                  {event.sequence} · {event.stage}
                </strong>{" "}
                · {event.state}
                <span className="muted">{event.timestamp}</span>
                {event.counters?.map((c) => (
                  <span key={c.name}>
                    {c.name === "accepted_pdf_pages"
                      ? "Retained processing pages"
                      : c.name}
                    : {c.count}
                  </span>
                ))}
                {event.diagnostic_refs?.length > 0 && (
                  <JsonDetails
                    value={event.diagnostic_refs}
                    label="Event diagnostic references"
                  />
                )}
              </li>
            ))}
          </ol>
          {events.length === 0 && (
            <p>
              {terminal(job.state)
                ? "This job is terminal. Reload reads its persisted outcome."
                : "Waiting for new events. Progress counts appear only when reported."}
            </p>
          )}
          {streamError !== null && (
            <ErrorNotice
              error={streamError}
              retry={() => setStreamEpoch((v) => v + 1)}
              retryLabel="Reconnect events"
            />
          )}
          {actionError !== null && <ErrorNotice error={actionError} />}{" "}
          {retryReceipt && (
            <Status>
              Recovery request accepted · {retryReceipt.state} ·{" "}
              <RouteLink to={`/dashboard/jobs/${objectPath(retryReceipt.id)}`}>
                Follow retry job
              </RouteLink>
            </Status>
          )}
        </>
      )}
    </>
  );
}
export function Jobs({ id }: { id?: string }) {
  return id ? <JobDetail key={id} id={id} /> : <JobList />;
}
function JobList() {
  const { adminClient } = useSession();
  const data = usePages<JobPage>("/v1/jobs", adminClient);
  return (
    <>
      <PageHeading title="Jobs">
        <p>
          Durable source and maintenance operations. Query history lives in{" "}
          <RouteLink to="/dashboard/activity">Activity</RouteLink>.
        </p>
      </PageHeading>
      <LoadState {...data} retry={() => void data.load()} />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th>Operation</th>
              <th>State</th>
              <th>Retries</th>
              <th>Reported error</th>
            </tr>
          </thead>
          <tbody>
            {data.pages
              .flatMap((p) => p.jobs)
              .map((job, i) => (
                <tr key={`${job.id}-${i}`}>
                  <td>
                    <RouteLink to={`/dashboard/jobs/${objectPath(job.id)}`}>
                      {job.id}
                    </RouteLink>
                  </td>
                  <td>{job.operation}</td>
                  <td>{job.state}</td>
                  <td>{job.retry_count}</td>
                  <td>{job.error_code ?? "None reported"}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      {!data.busy &&
        data.pages.length > 0 &&
        data.pages.every((p) => !p.jobs.length) && (
          <p>No jobs in this scope.</p>
        )}
      <More {...data} onClick={() => void data.more()} label="Load more jobs" />
    </>
  );
}
