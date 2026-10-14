import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSession } from "../../session";
import { useResource, usePages } from "../../hooks/useResource";
import type { Discovery, Settings } from "../../api/contracts";
import type { JobHandle } from "../../api/operations";
import { ApiError, publicError } from "../../api/client";
import { ErrorNotice, Status } from "../../components/Status";
import { JsonDetails } from "../../components/JsonDetails";
import { More, RouteLink } from "../../components/Operations";
import {
  encodedBody,
  fileBase64,
  fileFormat,
  parseMetadata,
  REQUEST_LIMIT,
  SubmissionIntent,
} from "./submission";
export function SourceForm() {
  const { client, selectedCollections, handshake } = useSession();
  const kinds = usePages<Discovery>("/v1/discovery?subject=kinds");
  const settings = useResource<Settings>("/v1/settings");
  const [type, setType] = useState("text");
  const [key, setKey] = useState("");
  const [kind, setKind] = useState("");
  const [collection, setCollection] = useState(selectedCollections[0]);
  const [format, setFormat] = useState("");
  const [text, setText] = useState("");
  const [path, setPath] = useState("");
  const [bytes, setBytes] = useState<string | null>(null);
  const [fileName, setFileName] = useState("");
  const [fileSize, setFileSize] = useState<number | null>(null);
  const [metadata, setMetadata] = useState("{}");
  const [title, setTitle] = useState("");
  const [publisher, setPublisher] = useState("");
  const [edition, setEdition] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [fileBusy, setFileBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [fileError, setFileError] = useState<unknown>(null);
  const [receipt, setReceipt] = useState<JobHandle | null>(null);
  const intent = useRef(new SubmissionIntent());
  const lifetime = useRef(new AbortController());
  const fileVersion = useRef(0);
  useEffect(
    () => () => {
      lifetime.current.abort();
      fileVersion.current++;
    },
    [],
  );
  const changed = () => {
    setConfirmed(false);
    setReceipt(null);
  };
  const resolvedFormat =
    format.trim() ||
    (type === "text" ? "text" : type === "bytes" ? fileFormat(fileName) : "");
  let encoded: { body: string; size: number } | null = null;
  let validation: unknown = null;
  try {
    if (type === "bytes" && bytes !== null && !resolvedFormat)
      throw new ApiError(
        "INVALID_REQUEST",
        "Choose an explicit format for this file; its filename has no recognized parser format.",
      );
    const parsedMetadata = parseMetadata(metadata, {
      title,
      publisher,
      edition,
    });
    if (type !== "bytes" || bytes !== null)
      encoded = encodedBody({
        type,
        source_key: key,
        collection_id: collection,
        kind,
        metadata: parsedMetadata,
        ...(resolvedFormat ? { format: resolvedFormat } : {}),
        ...(type === "text"
          ? { text }
          : type === "bytes"
            ? { data: bytes ?? "" }
            : { path }),
      });
  } catch (e) {
    validation = e;
  }
  const tooLarge = (encoded?.size ?? 0) >= REQUEST_LIMIT;
  const canSubmit =
    !!client &&
    confirmed &&
    encoded !== null &&
    validation === null &&
    !tooLarge &&
    !!key &&
    !!kind &&
    !busy &&
    (type !== "bytes" || (!fileBusy && fileError === null && bytes !== null)) &&
    !settings.busy &&
    settings.value !== null &&
    settings.error === null;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!canSubmit || !client || !encoded) return;
    const body = encoded.body;
    setBusy(true);
    setError(null);
    try {
      const value = await client.request<JobHandle>("/v1/sources", {
        method: "POST",
        body,
        headers: { "Idempotency-Key": intent.current.keyFor(body) },
        signal: lifetime.current.signal,
      });
      if (!lifetime.current.signal.aborted) {
        setReceipt(value);
        setConfirmed(false);
      }
    } catch (e) {
      if (!lifetime.current.signal.aborted) setError(publicError(e));
    } finally {
      if (!lifetime.current.signal.aborted) setBusy(false);
    }
  };
  const readFile = async (file: File | undefined) => {
    changed();
    const version = ++fileVersion.current;
    setBytes(null);
    setFileError(null);
    setFileName(file?.name ?? "");
    setFileSize(file?.size ?? null);
    setFileBusy(!!file);
    if (!file) return;
    try {
      const data = await fileBase64(file);
      if (version === fileVersion.current && !lifetime.current.signal.aborted)
        setBytes(data);
    } catch (e) {
      if (version === fileVersion.current && !lifetime.current.signal.aborted)
        setFileError(publicError(e));
    } finally {
      if (version === fileVersion.current && !lifetime.current.signal.aborted)
        setFileBusy(false);
    }
  };
  const definitions = kinds.pages.flatMap((p) => p.definitions);
  const selected = definitions.find((d) => d.id === kind);
  return (
    <section className="operation-panel">
      <h2>Add a source</h2>
      <form onSubmit={(e) => void submit(e)}>
        <fieldset disabled={busy}>
          <div className="field-grid">
            <label>
              Input type
              <select
                value={type}
                onChange={(e) => {
                  changed();
                  fileVersion.current++;
                  setBytes(null);
                  setFileError(null);
                  setFileBusy(false);
                  setFileName("");
                  setFileSize(null);
                  setType(e.target.value);
                }}
              >
                <option value="text">Pasted text</option>
                <option value="bytes">Chosen file bytes</option>
                <option value="file">Registered-root file path</option>
                <option value="folder">Registered-root folder path</option>
                <option value="repository">
                  Registered-root repository path
                </option>
              </select>
            </label>
            <label>
              Source key
              <input
                value={key}
                onChange={(e) => {
                  changed();
                  setKey(e.target.value);
                }}
                required
              />
            </label>
            <label>
              Collection
              <select
                value={collection}
                onChange={(e) => {
                  changed();
                  setCollection(e.target.value);
                }}
              >
                {selectedCollections.map((id) => (
                  <option key={id}>{id}</option>
                ))}
              </select>
            </label>
            <label>
              Kind
              <input
                list="source-kinds"
                value={kind}
                onChange={(e) => {
                  changed();
                  setKind(e.target.value);
                }}
                required
              />
              <datalist id="source-kinds">
                {definitions.map((d, i) => (
                  <option key={`${d.id}-${i}`} value={d.id}>
                    {d.label}
                  </option>
                ))}
              </datalist>
            </label>
            <label>
              Format
              <input
                value={format}
                onChange={(e) => {
                  changed();
                  setFormat(e.target.value);
                }}
                placeholder={
                  resolvedFormat
                    ? `Automatic: ${resolvedFormat}`
                    : "Enter an explicit parser format"
                }
              />
            </label>
            <label>
              Title
              <input
                value={title}
                onChange={(e) => {
                  changed();
                  setTitle(e.target.value);
                }}
              />
            </label>
            <label>
              Publisher
              <input
                value={publisher}
                onChange={(e) => {
                  changed();
                  setPublisher(e.target.value);
                }}
              />
            </label>
            <label>
              Edition
              <input
                value={edition}
                onChange={(e) => {
                  changed();
                  setEdition(e.target.value);
                }}
              />
            </label>
          </div>
          {type === "text" ? (
            <label>
              Source text
              <textarea
                value={text}
                onChange={(e) => {
                  changed();
                  setText(e.target.value);
                }}
                rows={8}
              />
            </label>
          ) : type === "bytes" ? (
            <>
              <label>
                Source file
                <input
                  type="file"
                  onChange={(e) => void readFile(e.target.files?.[0])}
                />
              </label>
              {fileSize !== null && (
                <p>
                  Selected file: {fileName} · {fileSize.toLocaleString("en-US")}{" "}
                  bytes.
                </p>
              )}
            </>
          ) : (
            <>
              <label>
                Registered-root path
                <input
                  value={path}
                  onChange={(e) => {
                    changed();
                    setPath(e.target.value);
                  }}
                  required
                />
              </label>
              <p className="muted">
                Enter an explicit server path within a registered root. The
                server validates and acquires this path.
              </p>
            </>
          )}
          <label>
            Metadata JSON
            <textarea
              value={metadata}
              onChange={(e) => {
                changed();
                setMetadata(e.target.value);
              }}
              rows={4}
              spellCheck={false}
            />
          </label>
          {selected && (
            <JsonDetails
              value={selected.definition}
              label="Selected kind requirements"
            />
          )}
          <More
            {...kinds}
            onClick={() => void kinds.more()}
            label="Load more kinds"
          />
          <div className="provider-context">
            <p>Active profile: {settings.value?.profile ?? "Unknown"}</p>
            {Object.entries(settings.value?.values.providers ?? {}).map(
              ([name, p]) => (
                <p key={name}>
                  {name}: {p.provider}
                  {p.model ? ` / ${p.model}` : ""}
                  {p.enabled === false ? " · disabled" : ""}
                </p>
              ),
            )}
            <p>
              Upload grant:{" "}
              {handshake?.scope.upload_providers.join(", ") || "None"}. Provider
              configuration is not upload consent.
            </p>
          </div>
          {settings.error !== null && <ErrorNotice error={settings.error} />}{" "}
          {kinds.error !== null && <ErrorNotice error={kinds.error} />}
          <p>
            Encoded request: {encoded?.size ?? "Unknown"} bytes · must be below
            1 MiB (1,048,576 bytes).
          </p>
          {tooLarge && (
            <Status tone="warning">
              The encoded request exceeds the 1 MiB limit. Reduce the input or
              use a registered-root file path for a file already on the server.
            </Status>
          )}
          {validation !== null && <ErrorNotice error={validation} />}{" "}
          {type === "bytes" && fileError !== null && (
            <ErrorNotice error={fileError} />
          )}
          <label className="collection-option">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            I confirm this source and processing destination.
          </label>
          <button className="primary" disabled={!canSubmit} type="submit">
            {busy ? "Submitting source…" : "Submit source"}
          </button>
          <p className="muted">
            Effective format:{" "}
            {resolvedFormat ||
              (type === "bytes"
                ? "Explicit format required for unrecognized filenames"
                : "Inferred by the server from registered path")}
            . Pasted text defaults to text; a deliberate format overrides this
            default.
          </p>
        </fieldset>
      </form>
      {error !== null && <ErrorNotice error={error} />}{" "}
      {receipt && (
        <Status tone="success">
          Accepted · {receipt.state} ·{" "}
          <RouteLink to={`/dashboard/jobs/${encodeURIComponent(receipt.id)}`}>
            Follow job
          </RouteLink>
        </Status>
      )}
    </section>
  );
}
