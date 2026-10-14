import { useState } from "react";
import { useSession } from "../../session";
import type { QueryResult, Segment } from "../../api/contracts";
import { EvidenceReader } from "../../components/EvidenceReader";
import { JsonDetails } from "../../components/JsonDetails";
/** Evidence selection claims only the membership returned by the retained query. */
export function QueryInspection({ result }: { result: QueryResult }) {
  const { client } = useSession();
  const [selection, setSelection] = useState<{
    id: string;
    segment: Segment;
  } | null>(null);
  return (
    <section className="operation-panel">
      <h3>Query evidence</h3>
      <p>
        Coverage: {result.coverage.status} · Run {result.run_id}
      </p>
      <ul className="compact-list">
        {result.segments.map((s) => (
          <li key={s.segment_id}>
            <p className="evidence-text">{s.text}</p>
            <div className="unit-actions">
              {[...s.matched_unit_ids, ...s.context_only_unit_ids].map((id) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => setSelection({ id, segment: s })}
                >
                  {s.matched_unit_ids.includes(id) ? "Matched" : "Context"} ·{" "}
                  {id}
                </button>
              ))}
            </div>
          </li>
        ))}
      </ul>
      {selection && client && (
        <EvidenceReader
          key={selection.id}
          client={client}
          unitId={selection.id}
          sourceId={selection.segment.source_id}
          revisionId={selection.segment.revision_id}
          citation={selection.segment.citations.find(
            (c) => c.unit_id === selection.id,
          )}
          selectionRole={
            selection.segment.matched_unit_ids.includes(selection.id)
              ? "matched"
              : "context"
          }
        />
      )}
      <p className="muted">
        {result.trace === null
          ? "Trace details are unavailable."
          : "Trace is included in the result JSON."}
      </p>
      <JsonDetails value={result} label="Full query response" />
    </section>
  );
}
