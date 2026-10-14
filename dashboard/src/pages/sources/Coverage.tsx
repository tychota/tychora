import { useState } from "react";
import { usePages } from "../../hooks/useResource";
import {
  objectPath,
  type RevisionPage,
  type ExtractionPage,
  type ChannelPage,
} from "../../api/operations";
import { LoadState, More, RouteLink } from "../../components/Operations";
import { JsonDetails } from "../../components/JsonDetails";
import { OriginalInspection } from "../../components/EvidenceReader";
import { useSession } from "../../session";
export function Channels({ path }: { path: string }) {
  const data = usePages<ChannelPage>(`${path}/channels`);
  return (
    <section>
      <h4>Retrieval channels</h4>
      <LoadState {...data} retry={() => void data.load()} />
      <ul className="compact-list">
        {data.pages
          .flatMap((p) => p.channels)
          .map((c, i) => (
            <li key={`${c.family_id}-${c.channel}-${i}`}>
              <strong>{c.channel}</strong> · {c.state}
              <span className="muted">Family {c.family_id}</span>
            </li>
          ))}
      </ul>
      {!data.busy &&
        data.pages.length > 0 &&
        data.pages.every((p) => p.channels.length === 0) && (
          <p>No retrieval channels reported. Counts are unknown.</p>
        )}
      <More
        {...data}
        onClick={() => void data.more()}
        label="Load more channels"
      />
    </section>
  );
}
export function RevisionCoverage({
  sourceId,
  revisionId,
}: {
  sourceId: string;
  revisionId: string;
}) {
  const { client } = useSession();
  const path = `/v1/sources/${objectPath(sourceId)}/revisions/${objectPath(revisionId)}`;
  const data = usePages<ExtractionPage>(`${path}/extractions`);
  return (
    <section className="operation-panel">
      <h2>Selected revision</h2>
      <p className="object-id">{revisionId}</p>
      {client && (
        <OriginalInspection
          key={revisionId}
          client={client}
          revisionId={revisionId}
        />
      )}
      <h3>Extraction coverage</h3>
      <LoadState {...data} retry={() => void data.load()} />
      {data.pages
        .flatMap((p) => p.extractions)
        .map((e, i) => (
          <article key={`${e.extraction_id}-${i}`}>
            <h4 className="object-id">{e.extraction_id}</h4>
            <JsonDetails
              value={e.stages}
              label="Extraction stages and counts"
            />
            {e.code_resolution && (
              <JsonDetails
                value={e.code_resolution}
                label="Code resolution diagnostics"
              />
            )}
            {e.channels_truncated && (
              <p>
                Embedded channels are truncated. Inspect the paginated channels
                below.
              </p>
            )}
            <Channels
              path={`${path}/extractions/${objectPath(e.extraction_id)}`}
            />
          </article>
        ))}
      <More
        {...data}
        onClick={() => void data.more()}
        label="Load more extractions"
      />
      {!data.busy &&
        data.pages.length > 0 &&
        data.pages.every((p) => p.extractions.length === 0) && (
          <p>No retained extractions reported.</p>
        )}
    </section>
  );
}
export function SourceRevisions({ sourceId }: { sourceId: string }) {
  const data = usePages<RevisionPage>(
    `/v1/sources/${objectPath(sourceId)}/revisions`,
  );
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <section className="operation-panel">
      <h2>Source revisions</h2>
      <p className="object-id">{sourceId}</p>
      <p className="muted">
        Revision order does not establish which revision is active.
      </p>
      <LoadState {...data} retry={() => void data.load()} />
      {data.pages.some((p) => p.scope_limited) && (
        <p>Coverage is limited by your selected scope.</p>
      )}
      {data.pages
        .flatMap((p) => p.revisions)
        .map((r, i) => (
          <article key={`${r.revision_id}-${i}`}>
            <h3 className="object-id">{r.revision_id}</h3>
            <p>{r.kind_id}</p>
            <JsonDetails
              value={r.stages}
              label="Revision capture diagnostics"
            />
            <p>
              {r.extractions_truncated
                ? "Embedded extractions are truncated."
                : "Embedded extraction summary."}{" "}
              Counts not reported remain unknown.
            </p>
            <div className="unit-actions">
              <button type="button" onClick={() => setSelected(r.revision_id)}>
                Inspect coverage
              </button>
              <RouteLink
                to={`/dashboard/sources/${objectPath(sourceId)}/revisions/${objectPath(r.revision_id)}`}
              >
                Open revision
              </RouteLink>
            </div>
          </article>
        ))}
      <More
        {...data}
        onClick={() => void data.more()}
        label="Load more revisions"
      />
      {selected && (
        <RevisionCoverage
          key={selected}
          sourceId={sourceId}
          revisionId={selected}
        />
      )}
    </section>
  );
}
