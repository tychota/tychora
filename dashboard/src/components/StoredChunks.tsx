import type { ExtractionWindowsPage, Segment } from "../api/contracts";
import { ApiClient } from "../api/client";
import { usePages } from "../hooks/useResource";
import { extractionPath } from "../pages/documents/SourceContent";
import { LoadState, More } from "./Operations";
import { LocatorDetails } from "./EvidenceReader";
import { JsonDetails } from "./JsonDetails";
export function StoredChunks({
  client,
  segment,
  unitId,
}: {
  client: ApiClient;
  segment: Segment;
  unitId: string | null;
}) {
  const anchors = segment.anchors.filter(
    (a) => !unitId || a.unit_id === unitId,
  );
  const anchor = anchors.find((a) => a.indexed_window_id !== null);
  const params = anchor
    ? new URLSearchParams({
        unit_id: anchor.unit_id,
        chunk_id: anchor.indexed_window_id!,
      })
    : null;
  const path = anchor
    ? `${extractionPath({ sourceId: segment.source_id, revisionId: segment.revision_id, extractionId: segment.extraction_id })}/windows?${params}`
    : null;
  const data = usePages<ExtractionWindowsPage>(path, client);
  return (
    <section aria-label="Stored chunks" className="passage-panel">
      <h2>Stored chunks</h2>
      <p className="muted">
        Retained chunk text and bounds from this extraction. Storage does not
        prove membership in the active index.
      </p>
      {!anchor ? (
        <p>No stored chunk identity was returned for this selection.</p>
      ) : (
        <>
          <p>
            Returned anchor · characters {anchor.char_start}–{anchor.char_end}
          </p>
          <LoadState {...data} retry={() => void data.load()} />
          {data.pages
            .flatMap((p) => p.windows)
            .map((w) => (
              <article key={w.id}>
                <p className="eyebrow">
                  Stored chunk · characters {w.char_start}–{w.char_end} ·{" "}
                  {w.token_count} reported tokens
                </p>
                <div className="evidence-text">{w.text}</div>
                <LocatorDetails locators={w.locators} />
                <JsonDetails
                  value={w}
                  label="Stored chunk identity and bounds"
                />
              </article>
            ))}
          {data.pages.some((p) => p.scope_limited) && (
            <p>Some candidates were excluded by this scope.</p>
          )}
          {!data.busy &&
            data.pages.length > 0 &&
            data.pages.every((p) => !p.windows.length) && (
              <p>
                No readable chunk returned.{" "}
                {data.next
                  ? "Continue to the next page."
                  : "The retained chunk may be unavailable in this scope."}
              </p>
            )}
          <More
            {...data}
            onClick={() => void data.more()}
            label="Load more chunks"
          />
          {data.error && (
            <button onClick={() => void data.load()}>
              Restart from first page
            </button>
          )}
        </>
      )}
    </section>
  );
}
