import { useEffect, useRef, useState } from "react";
import { ApiClient, publicError } from "../api/client";
import type { Citation, Inspection, Locator } from "../api/contracts";
import { ErrorNotice, Status } from "./Status";
import { JsonDetails, downloadBlob } from "./JsonDetails";

export function citationText(citation: Citation): string {
  return JSON.stringify(
    {
      source_ref: citation.source_ref,
      source_id: citation.source_id,
      revision_id: citation.revision_id,
      unit_id: citation.unit_id,
      asset_id: citation.asset_id,
      physical_page: citation.physical_page,
      printed_page: citation.printed_page,
      locators: citation.locators,
    },
    null,
    2,
  );
}
export function decodeBase64url(payload: string): Uint8Array<ArrayBuffer> {
  const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(
    normalized.padEnd(
      normalized.length + ((4 - (normalized.length % 4)) % 4),
      "=",
    ),
  );
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}
export function LocatorDetails({ locators }: { locators: Locator[] }) {
  return (
    <div className="locators">
      {locators.length ? (
        locators.map((locator, index) => (
          <div key={index}>
            <p>
              <strong>{locator.type}</strong> · {locator.native_ref}
              {"char_start" in locator &&
                ` · characters ${locator.char_start}–${locator.char_end}`}
              {locator.type === "code" &&
                ` · ${locator.relative_path}, lines ${locator.line_start}–${locator.line_end}, bytes ${locator.byte_start}–${locator.byte_end}`}
              {"physical_page" in locator &&
                locator.physical_page !== null &&
                ` · physical page ${locator.physical_page}`}
              {"printed_page" in locator &&
                locator.printed_page !== null &&
                ` · printed page ${locator.printed_page}`}
            </p>
            <JsonDetails value={locator} label="Exact locator" />
          </div>
        ))
      ) : (
        <p className="muted">No locator was returned.</p>
      )}
    </div>
  );
}
const rasterTypes = new Set([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
  "image/avif",
  "image/bmp",
]);
function OriginalContent({
  blob,
  mediaType,
}: {
  blob: Blob;
  mediaType: string;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [text, setText] = useState<string | null>(null);
  const mime = mediaType.split(";")[0].toLowerCase();
  useEffect(() => {
    const objectUrl = URL.createObjectURL(blob);
    setUrl(objectUrl);
    const reader = new FileReader();
    if (mime.startsWith("text/")) {
      reader.onload = () => setText(String(reader.result));
      reader.onerror = () =>
        setText(
          "Original text could not be decoded. Download the retained bytes.",
        );
      reader.readAsText(blob, "utf-8");
    }
    return () => {
      reader.abort();
      URL.revokeObjectURL(objectUrl);
    };
  }, [blob, mime]);
  return (
    <div className="original-content">
      <p className="eyebrow">Retained original · {mediaType}</p>
      {mime.startsWith("text/") ? (
        <pre className="original-text">{text ?? "Reading original text…"}</pre>
      ) : rasterTypes.has(mime) && url ? (
        <img
          className="original-image"
          src={url}
          alt="Retained original raster image"
        />
      ) : (
        <p>
          This media type is available as a download. Exact locators are
          retained above.
        </p>
      )}
      <button
        type="button"
        onClick={() => downloadBlob(blob, "retained-original")}
      >
        Download original bytes
      </button>
    </div>
  );
}
export interface OriginalInspectionProps {
  client: ApiClient;
  revisionId?: string;
  assetId?: string;
}
export function OriginalInspection({
  client,
  revisionId,
  assetId,
}: OriginalInspectionProps) {
  const [value, setValue] = useState<{
    blob: Blob;
    mediaType: string;
    truncated: boolean;
  } | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const request = useRef<AbortController | null>(null);
  useEffect(
    () => () => request.current?.abort(),
    [client, revisionId, assetId],
  );
  const load = async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    setUnavailable(false);
    setValue(null);
    try {
      if (assetId) {
        const blob = await client.blob(
          `/v1/assets/${encodeURIComponent(assetId)}/content`,
          { signal: controller.signal },
        );
        if (!controller.signal.aborted)
          setValue({
            blob,
            mediaType: blob.type || "application/octet-stream",
            truncated: false,
          });
      } else if (revisionId) {
        const result = await client.request<Inspection>("/v1/inspect", {
          method: "POST",
          signal: controller.signal,
          body: JSON.stringify({
            subject: "revisions",
            id: revisionId,
            original: true,
            view: "original",
          }),
        });
        if (controller.signal.aborted) return;
        if (result.payload === null) {
          setUnavailable(true);
          return;
        }
        const mediaType = result.media_type ?? "application/octet-stream";
        setValue({
          blob: new Blob([decodeBase64url(result.payload)], {
            type: mediaType,
          }),
          mediaType,
          truncated: result.truncated,
        });
      }
    } catch (error) {
      if (!controller.signal.aborted) setError(publicError(error));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  };
  if (!revisionId && !assetId)
    return <p className="muted">Original identity is unavailable.</p>;
  return (
    <section className="original-inspection" aria-label="Retained original">
      <button type="button" onClick={() => void load()} disabled={loading}>
        {loading ? "Loading original…" : "Inspect original"}
      </button>
      {error !== null && (
        <ErrorNotice error={error} retry={() => void load()} />
      )}
      {unavailable && (
        <Status tone="warning">Retained original is unavailable.</Status>
      )}
      {value && (
        <>
          {value.truncated && (
            <Status tone="warning">
              Original response is truncated; downloaded bytes contain only the
              returned payload.
            </Status>
          )}
          <OriginalContent
            key={`${revisionId}-${assetId}`}
            blob={value.blob}
            mediaType={value.mediaType}
          />
        </>
      )}
    </section>
  );
}
export interface EvidenceReaderProps {
  client: ApiClient;
  unitId: string;
  citation?: Citation;
  sourceId?: string;
  revisionId?: string;
  contextOnly?: boolean;
}
export function EvidenceReader({
  client,
  unitId,
  citation,
  sourceId,
  revisionId,
  contextOnly = false,
}: EvidenceReaderProps) {
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setInspection(null);
    setError(null);
    setCopied(false);
    setCopyError(false);
    void client
      .request<Inspection>(`/v1/evidence/${encodeURIComponent(unitId)}`, {
        signal: controller.signal,
      })
      .then((value) => {
        if (!controller.signal.aborted) setInspection(value);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setError(publicError(error));
      });
    return () => controller.abort();
  }, [client, unitId, attempt]);
  const unit = inspection?.evidence;
  const exactCitation =
    citation ??
    (unit
      ? {
          unit_id: unit.unit_id,
          asset_id: null,
          source_id: sourceId ?? null,
          revision_id: revisionId ?? null,
          source_ref: null,
          physical_page: null,
          printed_page: null,
          locators: unit.locators,
        }
      : null);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(citationText(exactCitation!));
      setCopied(true);
      setCopyError(false);
    } catch {
      setCopyError(true);
    }
  };
  return (
    <section className="evidence-reader" aria-label="Evidence reader">
      <div className="reader-heading">
        <p className="eyebrow">
          {contextOnly ? "Context only" : "Matched evidence"}
        </p>
        <h2>Exact evidence</h2>
      </div>
      {error !== null ? (
        <ErrorNotice error={error} retry={() => setAttempt(attempt + 1)} />
      ) : !inspection ? (
        <Status>Loading evidence…</Status>
      ) : (
        <>
          <dl className="identity">
            <dt>Evidence</dt>
            <dd>{inspection.id}</dd>
            <dt>Source</dt>
            <dd>{sourceId ?? citation?.source_id ?? "Unknown"}</dd>
            <dt>Revision</dt>
            <dd>{revisionId ?? citation?.revision_id ?? "Unknown"}</dd>
            <dt>Extraction</dt>
            <dd>
              {unit?.extraction_id ??
                inspection.partial_evidence?.extraction_id ??
                "Unknown"}
            </dd>
          </dl>
          {unit ? (
            <>
              <p className="origin-label">
                {unit.text_origin === "source"
                  ? "Original source text"
                  : unit.text_origin === "ocr"
                    ? "OCR text · derived from pixels"
                    : "Derived text"}{" "}
                · {unit.unit_type} · {unit.review_state}
                {!unit.citable && " · not citable"}
              </p>
              {unit.heading_path.length > 0 && (
                <p className="muted">{unit.heading_path.join(" / ")}</p>
              )}
              <div className="evidence-text">{unit.text}</div>
              <LocatorDetails locators={unit.locators} />
            </>
          ) : (
            <>
              <Status tone="warning">
                Full evidence is unavailable. Only returned partial metadata and
                excerpts can be inspected.
              </Status>
              {inspection.excerpts.map((excerpt, index) => (
                <div key={index}>
                  <p className="eyebrow">
                    Characters {excerpt.char_start}–{excerpt.char_end}
                  </p>
                  <p className="evidence-text">{excerpt.text}</p>
                </div>
              ))}
            </>
          )}
          {inspection.truncated && (
            <Status tone="warning">Evidence inspection is truncated.</Status>
          )}
          {exactCitation && (
            <div className="citation-actions">
              <button type="button" onClick={() => void copy()}>
                Copy citation
              </button>
              {copied && <span role="status">Citation copied.</span>}
              {copyError && (
                <span role="status">
                  Clipboard unavailable. Select the citation below to copy.
                </span>
              )}
              <JsonDetails
                value={exactCitation}
                label="Citation identity and locators"
              />
            </div>
          )}
          <OriginalInspection
            key={`${unitId}-original`}
            client={client}
            revisionId={revisionId ?? citation?.revision_id ?? undefined}
          />
          <JsonDetails value={inspection} label="Evidence response JSON" />
        </>
      )}
    </section>
  );
}
