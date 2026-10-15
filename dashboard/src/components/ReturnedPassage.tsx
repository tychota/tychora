import type { Segment } from "../api/contracts";
import { JsonDetails } from "./JsonDetails";
import { Status } from "./Status";
export function ReturnedPassage({ segment }: { segment: Segment }) {
  return (
    <section aria-label="Returned passage" className="passage-panel">
      <h2>Returned passage</h2>
      <p className="muted">
        Exact blocks returned by this query. Full unit text is a separate
        inspection.
      </p>
      {segment.excerpts.map((e, i) => (
        <article key={`match-${i}`}>
          <p className="eyebrow">
            {segment.matched_unit_ids.includes(e.unit_id)
              ? "Matched evidence"
              : "Returned evidence"}{" "}
            · characters {e.char_start}–{e.char_end}
          </p>
          <div className="evidence-text">{e.text}</div>
        </article>
      ))}
      {segment.context_excerpts.map((e, i) => (
        <article key={`context-${i}`}>
          <p className="eyebrow">
            Context only · characters {e.char_start}–{e.char_end}
          </p>
          <div className="evidence-text">{e.text}</div>
        </article>
      ))}
      {!segment.excerpts.length && !segment.context_excerpts.length && (
        <p>No text excerpts returned. Structured records may be available.</p>
      )}
      <p>
        {segment.complete
          ? "Returned segment complete"
          : "Returned segment incomplete"}
        {segment.truncated ? " · truncated" : ""}
      </p>
      {segment.omissions.map((o, i) => (
        <Status tone="warning" key={i}>
          Omitted: {o.reason} · {o.count} reported items
        </Status>
      ))}
      {segment.structured_records.length > 0 && (
        <section>
          <h3>Structured records</h3>
          {segment.structured_records.map((r) => (
            <article key={r.record_id}>
              <p>
                {r.record_type} · {r.schema_id}
              </p>
              <pre className="evidence-text">
                {JSON.stringify(r.value, null, 2)}
              </pre>
            </article>
          ))}
        </section>
      )}
      <JsonDetails
        value={segment.anchors}
        label="Returned anchors and clipped ranges"
      />
      <JsonDetails value={segment.citations} label="Returned citations" />
    </section>
  );
}
