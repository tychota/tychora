import { useState } from "react";
import { useSession } from "../../session";
import { usePages } from "../../hooks/useResource";
import type {
  ExtractionUnitsPage,
  ExtractionWindowsPage,
} from "../../api/contracts";
import { objectPath } from "../../api/operations";
import { LoadState, More } from "../../components/Operations";
import {
  EvidenceReader,
  LocatorDetails,
} from "../../components/EvidenceReader";
import { JsonDetails } from "../../components/JsonDetails";
import { Relations } from "../../components/Relations";
export interface ContentLineage {
  sourceId: string;
  revisionId: string;
  extractionId: string;
}
export function extractionPath({
  sourceId,
  revisionId,
  extractionId,
}: ContentLineage) {
  return `/v1/sources/${objectPath(sourceId)}/revisions/${objectPath(revisionId)}/extractions/${objectPath(extractionId)}`;
}
export function SourceContent({
  lineage,
  view,
}: {
  lineage: ContentLineage;
  view: "content" | "chunks" | "relationships";
}) {
  const { client } = useSession();
  const [fullUnit, setFullUnit] = useState<string | null>(null);
  const units = usePages<ExtractionUnitsPage>(
    view === "chunks" ? null : `${extractionPath(lineage)}/units`,
  );
  const windows = usePages<ExtractionWindowsPage>(
    view === "chunks" ? `${extractionPath(lineage)}/windows` : null,
  );
  const data = view === "chunks" ? windows : units;
  const count =
    view === "chunks"
      ? windows.pages.flatMap((p) => p.windows).length
      : units.pages.flatMap((p) => p.units).length;
  return (
    <section
      aria-label={
        view === "chunks"
          ? "Stored chunks"
          : view === "relationships"
            ? "Document relationships"
            : "Extracted content"
      }
    >
      <p className="muted">
        {view === "chunks"
          ? "Stored chunks from this extraction. Search may use a different extraction."
          : "Source-ordered unit previews are bounded. Open Full unit to deliberately inspect complete authorized text."}
      </p>
      <LoadState
        {...data}
        retry={() => {
          setFullUnit(null);
          void data.load();
        }}
      />
      {data.pages.some((p) => p.scope_limited) && (
        <p>
          Some candidates were excluded by this scope. Continue through
          available pages.
        </p>
      )}
      {view === "chunks"
        ? windows.pages
            .flatMap((p) => p.windows)
            .map((w) => (
              <article className="content-unit" key={w.id}>
                <p className="eyebrow">
                  Stored chunk · characters {w.char_start}–{w.char_end} ·{" "}
                  {w.token_count} reported tokens
                </p>
                <div className="evidence-text">{w.text}</div>
                <LocatorDetails locators={w.locators} />
                <button onClick={() => setFullUnit(w.unit_id)}>
                  Full unit
                </button>
                <JsonDetails
                  value={{
                    id: w.id,
                    unit_id: w.unit_id,
                    extraction_id: w.extraction_id,
                    char_start: w.char_start,
                    char_end: w.char_end,
                    token_count: w.token_count,
                    chunker_fingerprint: w.chunker_fingerprint,
                    context_group_key: w.context_group_key,
                  }}
                  label="Chunk identity and bounds"
                />
              </article>
            ))
        : view === "relationships"
          ? client && (
              <Relations
                client={client}
                unitIds={units.pages.flatMap((p) => p.units).map((u) => u.id)}
                onSelectUnit={setFullUnit}
              />
            )
          : units.pages
              .flatMap((p) => p.units)
              .map((u) => (
                <article className="content-unit" key={u.id}>
                  <h3>{u.heading_path.join(" / ") || u.unit_type}</h3>
                  <p className="muted">
                    {u.text_origin === "ocr"
                      ? "OCR text · derived from pixels"
                      : u.text_origin === "source"
                        ? "Original source text"
                        : "Derived text"}{" "}
                    · {u.review_state} · {u.citable ? "Citable" : "Not citable"}
                  </p>
                  <div className="evidence-text">{u.preview}</div>
                  <LocatorDetails locators={u.locators} />
                  <button onClick={() => setFullUnit(u.id)}>Full unit</button>
                  <JsonDetails
                    value={{
                      id: u.id,
                      ordinal: u.ordinal,
                      unit_type: u.unit_type,
                    }}
                    label="Unit identity"
                  />
                </article>
              ))}
      {!data.busy && data.pages.length > 0 && !count && (
        <p>
          {data.next
            ? "No readable items on this page. Continue to the next page."
            : "No readable items were returned for this extraction and scope."}
        </p>
      )}
      <More
        {...data}
        onClick={() => void data.more()}
        label={view === "chunks" ? "Load more chunks" : "Load more units"}
      />
      {data.error instanceof Error &&
        "code" in data.error &&
        data.error.code === "INVALID_REQUEST" && (
          <button
            onClick={() => {
              setFullUnit(null);
              void data.load();
            }}
          >
            Restart from first page
          </button>
        )}
      {fullUnit && client && (
        <section className="full-unit">
          <div className="detail-toolbar">
            <h3>Full unit</h3>
            <button onClick={() => setFullUnit(null)}>Close full unit</button>
          </div>
          <EvidenceReader
            key={fullUnit}
            client={client}
            unitId={fullUnit}
            sourceId={lineage.sourceId}
            revisionId={lineage.revisionId}
          />
        </section>
      )}
    </section>
  );
}
