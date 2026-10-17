import { useState } from "react";
import type { ExtractionWindowsPage, VisualEvidence } from "../api/contracts";
import type { ApiClient } from "../api/client";
import { usePages } from "../hooks/useResource";
import { extractionPath } from "../pages/documents/SourceContent";
import { EvidenceReader, LocatorDetails } from "./EvidenceReader";
import { LoadState, More, RouteLink } from "./Operations";
import { JsonDetails } from "./JsonDetails";
function ProxyChunks({
  client,
  visual,
  unitId,
  extractionId,
}: {
  client: ApiClient;
  visual: VisualEvidence;
  unitId: string;
  extractionId: string;
}) {
  const data = usePages<ExtractionWindowsPage>(
    `${extractionPath({ sourceId: visual.source_id, revisionId: visual.revision_id, extractionId })}/windows?unit_id=${encodeURIComponent(unitId)}`,
    client,
  );
  return (
    <section aria-label="Stored proxy chunks">
      <h3>Stored chunks</h3>
      <p>
        No indexed-window anchor was returned for this visual proxy. These are
        retained chunks for the actual inspected unit, not proof of active index
        membership.
      </p>
      <LoadState {...data} retry={() => void data.load()} />
      {data.pages
        .flatMap((p) => p.windows)
        .map((w) => (
          <article key={w.id}>
            <p>
              Stored chunk · characters {w.char_start}–{w.char_end}
            </p>
            <div className="evidence-text">{w.text}</div>
            <LocatorDetails locators={w.locators} />
          </article>
        ))}
      {data.pages.some((p) => p.scope_limited) && (
        <p>Some stored candidates were excluded by this scope.</p>
      )}
      <More
        {...data}
        onClick={() => void data.more()}
        label="Load more proxy chunks"
      />
    </section>
  );
}
/** A returned labelled visual proxy is not a fabricated text Segment or citation. */
export function VisualPassage({
  client,
  visual,
}: {
  client: ApiClient;
  visual: VisualEvidence;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState("passage");
  const [extractionId, setExtractionId] = useState<string | null>(null);
  const proxy = visual.proxies.find((p) => p.proxy_id === selected);
  const select = async (id: string) => {
    setSelected(id);
    setView("passage");
    setExtractionId(null);
  };
  // Full inspection is deliberate and obtains actual extraction lineage for chunk browsing.
  return (
    <section aria-label="Returned visual proxies">
      {visual.proxies.map((p) => (
        <button
          key={p.proxy_id}
          aria-pressed={selected === p.proxy_id}
          onClick={() => void select(p.proxy_id)}
        >
          Inspect returned {p.label} proxy
        </button>
      ))}
      {proxy && (
        <>
          <div className="detail-toolbar">
            <button
              aria-pressed={view === "passage"}
              onClick={() => setView("passage")}
            >
              Returned passage
            </button>
            <button
              aria-pressed={view === "chunks"}
              disabled={!extractionId}
              onClick={() => setView("chunks")}
            >
              Stored chunks
            </button>
            <button
              aria-pressed={view === "full"}
              onClick={() => setView("full")}
            >
              Full unit
            </button>
          </div>
          {view === "passage" && (
            <section aria-label="Returned proxy passage">
              <h3>Returned derived text</h3>
              <p>
                {proxy.origin} · {proxy.review_state} · provenance reference; no
                native source quote is implied.
              </p>
              <div className="evidence-text">{proxy.text}</div>
              <p>
                Full unit inspection supplies authorized lineage before browsing
                stored chunks. No indexed-window ID is returned for this proxy.
              </p>
            </section>
          )}
          {view === "full" && (
            <EvidenceReader
              key={proxy.proxy_id}
              client={client}
              unitId={proxy.proxy_id}
              sourceId={visual.source_id}
              revisionId={visual.revision_id}
              onInspected={(value) =>
                setExtractionId(
                  value.evidence?.extraction_id ??
                    value.partial_evidence?.extraction_id ??
                    null,
                )
              }
            />
          )}
          {view === "chunks" && extractionId && (
            <ProxyChunks
              client={client}
              visual={visual}
              unitId={proxy.proxy_id}
              extractionId={extractionId}
            />
          )}
          <RouteLink
            to={`/dashboard/explore?unit=${encodeURIComponent(proxy.proxy_id)}`}
          >
            Explore this proxy
          </RouteLink>
          <JsonDetails value={proxy} label="Returned proxy provenance" />
        </>
      )}
    </section>
  );
}
