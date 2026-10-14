import { useEffect, useRef, useState } from "react";
import { ApiClient, publicError } from "../api/client";
import type { RelationResult } from "../api/contracts";
import { ErrorNotice, Status } from "./Status";
import { JsonDetails } from "./JsonDetails";

export interface RelationsProps {
  client: ApiClient;
  unitIds: string[];
  onSelectUnit?: (id: string) => void;
}
export function Relations({ client, unitIds, onSelectUnit }: RelationsProps) {
  const [direction, setDirection] = useState("outgoing");
  const [predicate, setPredicate] = useState("");
  const [view, setView] = useState("semantic");
  const [hops, setHops] = useState(1);
  const [result, setResult] = useState<RelationResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const identity = unitIds.join("|");
  useEffect(() => {
    setResult(null);
    setError(null);
    return () => request.current?.abort();
  }, [client, identity]);
  const explore = async (cursor?: string) => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const value = await client.request<RelationResult>("/v1/relations", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          unit_ids: unitIds.slice(0, 5),
          direction,
          view,
          predicates: predicate.trim() ? [predicate.trim()] : [],
          max_hops: hops,
          max_edges: 30,
          ...(cursor ? { cursor } : {}),
        }),
      });
      if (!controller.signal.aborted) setResult(value);
    } catch (error) {
      if (!controller.signal.aborted) setError(publicError(error));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  };
  const inspect = (id: string) => {
    setSelected(id);
    onSelectUnit?.(id);
  };
  return (
    <section className="relations" aria-label="Relations">
      <h3>Relations</h3>
      <p className="muted">
        Explicit exploration from selected evidence; at most 30 edges, up to two
        hops.
      </p>
      <div className="filter-row">
        <label>
          Direction
          <select
            value={direction}
            onChange={(e) => setDirection(e.target.value)}
          >
            <option value="outgoing">Outgoing</option>
            <option value="incoming">Incoming</option>
            <option value="both">Both</option>
          </select>
        </label>
        <label>
          Relation view
          <select value={view} onChange={(e) => setView(e.target.value)}>
            <option value="semantic">Semantic</option>
            <option value="structural">Structural</option>
          </select>
        </label>
        <label>
          Hops
          <select
            value={hops}
            onChange={(e) => setHops(Number(e.target.value))}
          >
            <option value="1">1</option>
            <option value="2">2</option>
          </select>
        </label>
        <label>
          Predicate ID
          <input
            value={predicate}
            onChange={(e) => setPredicate(e.target.value)}
            placeholder="All permitted predicates"
          />
        </label>
      </div>
      <button
        type="button"
        onClick={() => void explore()}
        disabled={loading || !unitIds.length}
      >
        {loading ? "Exploring…" : "Explore relations"}
      </button>
      {error !== null && (
        <ErrorNotice error={error} retry={() => void explore()} />
      )}
      {result && (
        <>
          {result.paths.length === 0 && (
            <Status>
              No relation paths were returned. This may reflect scope or an
              unavailable relation channel.
            </Status>
          )}
          {result.truncated && (
            <Status tone="warning">
              Relation view is truncated to the returned paths.
            </Status>
          )}
          {result.paths.length > 0 && (
            <>
              <div
                className="relation-graph"
                aria-label="Returned relation paths"
              >
                {result.paths.map((path, index) => (
                  <div className="graph-path" key={index}>
                    {path.unit_ids.map((id, nodeIndex) => (
                      <span
                        className="graph-node-group"
                        key={`${id}-${nodeIndex}`}
                      >
                        {nodeIndex > 0 && (
                          <span className="graph-predicate">
                            →{" "}
                            {path.predicates[nodeIndex - 1] ??
                              "Predicate unavailable"}{" "}
                            →
                          </span>
                        )}
                        <button
                          type="button"
                          className={
                            selected === id
                              ? "graph-node selected"
                              : "graph-node"
                          }
                          aria-label={`Select graph unit ${id}`}
                          onClick={() => inspect(id)}
                        >
                          {id}
                        </button>
                      </span>
                    ))}
                  </div>
                ))}
              </div>
              <ol className="relation-list">
                {result.paths.map((path, index) => (
                  <li key={index}>
                    <p>
                      <strong>
                        {path.predicates.join(" → ") || "Predicate unavailable"}
                      </strong>
                    </p>
                    <div className="unit-actions">
                      {path.unit_ids.map((id, nodeIndex) => (
                        <button
                          key={`${id}-${nodeIndex}`}
                          type="button"
                          aria-label={`Inspect ${id}`}
                          className={selected === id ? "selected" : ""}
                          onClick={() => inspect(id)}
                        >
                          {id}
                        </button>
                      ))}
                    </div>
                    <p className="muted">
                      Assertions:{" "}
                      {path.assertion_ids.join(", ") || "None returned"}
                    </p>
                    <JsonDetails value={path} label="Relation provenance" />
                  </li>
                ))}
              </ol>
            </>
          )}
          {result.next_cursor && (
            <button
              type="button"
              onClick={() => void explore(result.next_cursor!)}
              disabled={loading}
            >
              Next relation page
            </button>
          )}
        </>
      )}
    </section>
  );
}
