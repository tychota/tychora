import { useEffect, useRef, useState } from "react";
import type { MaintenanceResult } from "../api/administration";
import { useSession } from "../session";
import { maintain } from "../api/administration";
import { ErrorNotice, Status } from "./Status";
import { JsonDetails } from "./JsonDetails";
import { RouteLink } from "./Operations";
/** Mutation lifetime is connection/route-owned, not selected-collection-owned. */
export function useAdministration() {
  const { adminClient } = useSession();
  const lifetime = useRef(new AbortController());
  useEffect(() => {
    lifetime.current = new AbortController();
    return () => lifetime.current.abort();
  }, [adminClient]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<MaintenanceResult | null>(null);
  const run = async (value: unknown) => {
    if (!adminClient || busy) return null;
    const signal = lifetime.current.signal;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const next = await maintain(adminClient, value, signal);
      if (!signal.aborted) {
        setResult(next);
        return next;
      }
    } catch (e) {
      if (!signal.aborted) setError(e);
    } finally {
      if (!signal.aborted) setBusy(false);
    }
    return null;
  };
  return { busy, error, result, run, setResult };
}
export function AdministrationResult({
  result,
  error,
}: {
  result: MaintenanceResult | null;
  error: unknown;
}) {
  return (
    <>
      {error !== null && <ErrorNotice error={error} />}
      {result && (
        <section aria-label="Operation result">
          <Status tone="success">
            Server result · {result.plan.operation}
          </Status>
          {result.job && (
            <p>
              Durable job · {result.job.state} ·{" "}
              <RouteLink
                to={`/dashboard/jobs/${encodeURIComponent(result.job.id)}`}
              >
                Follow job {result.job.id}
              </RouteLink>
            </p>
          )}
          {result.receipt && (
            <p>
              Receipt {result.receipt.receipt_id} · completed{" "}
              {result.receipt.completed_at}
              {result.receipt.manifest_sha &&
                ` · manifest ${result.receipt.manifest_sha}`}
            </p>
          )}
          {result.recovery && (
            <dl className="identity">
              {Object.entries(result.recovery).map(([k, v]) => (
                <div key={k}>
                  <dt>
                    {k}: {String(v)}
                  </dt>
                </div>
              ))}
            </dl>
          )}
          <JsonDetails
            value={result}
            label="Authoritative operation plan and result"
          />
        </section>
      )}
    </>
  );
}
