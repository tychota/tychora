import { useState } from "react";
import { useSession } from "../session";
import { usePages } from "../hooks/useResource";
import type { DocumentPage, ExtractionUnitsPage } from "../api/contracts";
import { extractionPath } from "./documents/SourceContent";
import { Relations } from "../components/Relations";
import { EvidenceReader } from "../components/EvidenceReader";
import {
  PageHeading,
  LoadState,
  More,
  RouteLink,
} from "../components/Operations";
export function Explore() {
  const { client } = useSession();
  const docs = usePages<DocumentPage>("/v1/documents");
  const params = new URLSearchParams(window.location.search);
  const [source, setSource] = useState(params.get("source") || "");
  const [unit, setUnit] = useState<string | null>(params.get("unit"));
  const [inspect, setInspect] = useState<string | null>(null);
  const selected = docs.pages
    .flatMap((p) => p.documents)
    .find((d) => d.source_id === source);
  const lineage =
    selected?.revision_id && selected.extraction_id
      ? {
          sourceId: selected.source_id,
          revisionId: selected.revision_id,
          extractionId: selected.extraction_id,
        }
      : null;
  const units = usePages<ExtractionUnitsPage>(
    lineage ? `${extractionPath(lineage)}/units` : null,
  );
  return (
    <>
      <PageHeading title="Explore">
        <p>
          Bounded relationships from selected evidence. Only returned paths are
          shown; coverage of the full corpus is unknown.
        </p>
      </PageHeading>
      <label>
        Selected document
        <select
          value={source}
          onChange={(e) => {
            setSource(e.target.value);
            setUnit(null);
            setInspect(null);
          }}
        >
          <option value="">Choose a loaded document</option>
          {docs.pages
            .flatMap((p) => p.documents)
            .map((d) => (
              <option key={d.source_id} value={d.source_id}>
                {d.title || d.source_key}
              </option>
            ))}
        </select>
      </label>
      <LoadState {...docs} retry={() => void docs.load()} />
      <More
        {...docs}
        onClick={() => void docs.more()}
        label="Load more documents"
      />
      {lineage && (
        <>
          <label>
            Seed unit
            <select
              value={unit || ""}
              onChange={(e) => {
                setUnit(e.target.value || null);
                setInspect(null);
              }}
            >
              <option value="">Choose actual extracted content</option>
              {units.pages
                .flatMap((p) => p.units)
                .map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.heading_path.join(" / ") || u.unit_type} ·{" "}
                    {u.preview.slice(0, 80)}
                  </option>
                ))}
            </select>
          </label>
          <LoadState {...units} retry={() => void units.load()} />
          <More
            {...units}
            onClick={() => void units.more()}
            label="Load more seed units"
          />
        </>
      )}
      {unit && client ? (
        <>
          <p>Selected seed: {unit}</p>
          <Relations
            key={unit}
            client={client}
            unitIds={[unit]}
            onSelectUnit={setInspect}
          />
          {inspect && (
            <EvidenceReader
              key={inspect}
              client={client}
              unitId={inspect}
              selectionRole="related"
            />
          )}
        </>
      ) : (
        <p>
          Select a source unit here or follow a selected passage from{" "}
          <RouteLink to="/dashboard/search">Search</RouteLink>. No global graph
          is implied.
        </p>
      )}
    </>
  );
}
