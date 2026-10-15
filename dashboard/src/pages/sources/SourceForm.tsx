import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSession } from "../../session";
import { useResource, usePages } from "../../hooks/useResource";
import type {
  Discovery,
  Settings,
  CollectionPage,
  KindMetadataForm,
  ModelCatalog,
  ModelReadiness,
  PdfIntakeDescriptor,
  Json,
} from "../../api/contracts";
import type { JobHandle } from "../../api/operations";
import { ApiError, publicError } from "../../api/client";
import { ErrorNotice, Status } from "../../components/Status";
import { JsonDetails } from "../../components/JsonDetails";
import { LoadState, More, RouteLink } from "../../components/Operations";
import { useAddDocumentDraft } from "../documents/draft";
import { ingestionPrerequisites, requiredAssetSelections } from "./readiness";
import {
  encodedBody,
  fileBase64,
  fileFormat,
  parseMetadata,
  REQUEST_LIMIT,
} from "./submission";
import { PdfIntake } from "./PdfIntakePanel";
const labelFor = (name: string) =>
  name.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
export function SourceForm() {
  const session = useSession();
  const { adminClient, handshake } = session;
  const { draft, setDraft, draftId, intent, receiptOwner, pendingPdf } =
    useAddDocumentDraft();
  const kinds = usePages<Discovery>("/v1/discovery?subject=kinds", adminClient);
  const settings = useResource<Settings>("/v1/settings", adminClient);
  const collections = usePages<CollectionPage>(
    "/v1/collections?include_archived=false",
    adminClient,
  );
  const schema = useResource<KindMetadataForm>(
    draft.kind
      ? `/v1/discovery/kinds/${encodeURIComponent(draft.kind)}/metadata-schema`
      : null,
    adminClient,
  );
  const manager = handshake?.scope.capabilities.includes("manage") === true;
  const catalog = useResource<ModelCatalog>(
    manager ? "/v1/models" : null,
    adminClient,
  );
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [fileBusy, setFileBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [fileError, setFileError] = useState<unknown>(null);
  const lifetime = useRef(new AbortController());
  const fileVersion = useRef(0);
  useEffect(
    () => () => {
      lifetime.current.abort();
      fileVersion.current++;
    },
    [],
  );
  const update = (patch: Partial<typeof draft>) => {
    setConfirmed(false);
    setDraft((previous) => ({ ...previous, ...patch, receipt: null }));
  };
  const resolvedFormat =
    draft.format.trim() ||
    (draft.type === "text"
      ? "text"
      : draft.type === "bytes"
        ? fileFormat(draft.file?.name ?? "")
        : "");
  const isPdf = draft.type === "bytes" && resolvedFormat === "pdf";
  const selections = requiredAssetSelections(catalog.value, resolvedFormat);
  const readinessPath =
    manager && selections.length
      ? `/v1/models/readiness?${selections.map((s) => `selection=${encodeURIComponent(s)}`).join("&")}`
      : null;
  const readiness = useResource<ModelReadiness>(readinessPath, adminClient);
  const prerequisites = ingestionPrerequisites(
    catalog.value,
    readiness.value,
    handshake,
    draft.collection,
    selections,
  );
  const schemaValue =
    schema.value?.kind_id === draft.kind ? schema.value : null;
  const required = schemaValue?.fields.filter((f) => f.required) ?? [];
  const active = collections.pages
    .flatMap((p) => p.collections)
    .filter(
      (c) =>
        !c.archived_at &&
        handshake?.scope.collection_ids.includes(c.collection_id),
    );
  const destination = active.find((c) => c.collection_id === draft.collection);
  let resolvedMetadata: Record<string, unknown> = {};
  let encoded: { body: string; size: number } | null = null;
  let validation: unknown = null;
  try {
    if (
      draft.type === "bytes" &&
      draft.file &&
      (fileFormat(draft.file.name) === "pdf" ||
        draft.file.type === "application/pdf") &&
      resolvedFormat !== "pdf"
    )
      throw new ApiError(
        "INVALID_REQUEST",
        "PDF files require PDF format and streamed analysis. Clear the conflicting format override.",
      );
    if (draft.type === "bytes" && draft.bytes !== null && !resolvedFormat)
      throw new ApiError(
        "INVALID_REQUEST",
        "Choose an explicit format for this file; its filename has no recognized parser format.",
      );
    const metadata = parseMetadata(draft.metadata, {});
    for (const [name, input] of Object.entries(draft.fields)) {
      const field = schemaValue?.fields.find((f) => f.name === name);
      if (!input.trim() && !field?.required) {
        delete metadata[name];
        continue;
      }
      if (field && !field.value_types.includes("string") && input.trim()) {
        if (field.value_types.includes("boolean"))
          metadata[name] = input === "true";
        else if (
          field.value_types.includes("integer") ||
          field.value_types.includes("number")
        ) {
          const number = Number(input);
          if (
            !Number.isFinite(number) ||
            (field.value_types.includes("integer") && !Number.isInteger(number))
          )
            throw new ApiError(
              "INVALID_REQUEST",
              `${labelFor(name)} requires a valid ${field.value_types.join(" / ")}.`,
            );
          metadata[name] = number;
        } else metadata[name] = input;
      } else metadata[name] = input;
    }
    resolvedMetadata = metadata;
    for (const field of required) {
      const value = metadata[field.name];
      const c = typeof field.constraints === "object" ? field.constraints : {};
      if (
        value === undefined ||
        value === "" ||
        (value === null && !field.value_types.includes("null"))
      )
        throw new ApiError(
          "INVALID_REQUEST",
          `${labelFor(field.name)} is required by ${schemaValue?.schema_id}.`,
        );
      if (
        typeof value === "string" &&
        ((typeof c.minLength === "number" && value.length < c.minLength) ||
          (typeof c.maxLength === "number" && value.length > c.maxLength))
      )
        throw new ApiError(
          "INVALID_REQUEST",
          `${labelFor(field.name)} does not meet the registered length constraint.`,
        );
    }
    if (draft.type !== "bytes" || draft.bytes !== null)
      encoded = encodedBody({
        type: draft.type,
        source_key: draft.key.trim() || draft.fields.title?.trim() || "",
        collection_id: draft.collection,
        kind: draft.kind,
        metadata,
        ...(resolvedFormat ? { format: resolvedFormat } : {}),
        ...(draft.type === "text"
          ? { text: draft.text }
          : draft.type === "bytes"
            ? { data: draft.bytes ?? "" }
            : { path: draft.path }),
      });
  } catch (e) {
    validation = e;
  }
  const tooLarge = (encoded?.size ?? 0) >= REQUEST_LIMIT;
  const canSubmit =
    !!adminClient &&
    handshake?.scope.capabilities.includes("write") &&
    !!destination &&
    !!draft.kind &&
    !!(draft.key.trim() || draft.fields.title?.trim()) &&
    schemaValue !== null &&
    (draft.type !== "text" || !!draft.text.trim()) &&
    ((draft.type === "text" && draft.kind === "document@1") || confirmed) &&
    !busy &&
    encoded !== null &&
    !validation &&
    !tooLarge &&
    (draft.type !== "bytes" ||
      (!fileBusy && fileError === null && draft.bytes !== null)) &&
    !settings.busy &&
    settings.value !== null &&
    settings.error === null &&
    !prerequisites.some((p) => p.blocking) &&
    !readiness.busy;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (isPdf || !canSubmit || !adminClient || !handshake || !encoded) return;
    const body = encoded.body;
    const submittedClient = adminClient
      .withGrant(handshake.scope)
      .narrow([draft.collection]);
    setBusy(true);
    setError(null);
    try {
      const receipt = await submittedClient.request<JobHandle>("/v1/sources", {
        method: "POST",
        body,
        headers: { "Idempotency-Key": intent.keyFor(body) },
        signal: lifetime.current.signal,
      });
      if (!lifetime.current.signal.aborted) {
        if (receiptOwner.current !== pendingPdf.current?.owner)
          receiptOwner.current?.dispose();
        receiptOwner.current = submittedClient;
        setDraft((previous) => ({ ...previous, receipt, setupReturn: null }));
        setConfirmed(false);
      }
    } catch (e) {
      if (!lifetime.current.signal.aborted) setError(publicError(e));
    } finally {
      if (receiptOwner.current !== submittedClient) submittedClient.dispose();
      if (!lifetime.current.signal.aborted) setBusy(false);
    }
  };
  const readFile = async (file: File | undefined) => {
    update({ bytes: null, file: file ?? null });
    const version = ++fileVersion.current;
    setFileError(null);
    setFileBusy(!!file);
    if (!file) return;
    if (
      (draft.format.trim() || fileFormat(file.name)) === "pdf" ||
      fileFormat(file.name) === "pdf" ||
      file.type === "application/pdf"
    ) {
      setFileBusy(false);
      return;
    }
    try {
      const bytes = await fileBase64(file);
      if (version === fileVersion.current && !lifetime.current.signal.aborted)
        setDraft((previous) => ({ ...previous, bytes }));
    } catch (e) {
      if (version === fileVersion.current && !lifetime.current.signal.aborted)
        setFileError(publicError(e));
    } finally {
      if (version === fileVersion.current && !lifetime.current.signal.aborted)
        setFileBusy(false);
    }
  };
  const switchType = (type: string) => {
    fileVersion.current++;
    setFileBusy(false);
    setFileError(null);
    update({ type, bytes: null, file: null, format: "" });
  };
  const setField = (name: string, value: string) =>
    update({ fields: { ...draft.fields, [name]: value } });
  const formField = (
    field: NonNullable<typeof schemaValue>["fields"][number],
  ) => {
    const c = typeof field.constraints === "object" ? field.constraints : {};
    if (field.advanced)
      return (
        <p key={field.name}>
          {labelFor(field.name)} is required. Enter its registered{" "}
          {field.value_types.join(" / ")} value in Metadata JSON below.
        </p>
      );
    if (
      !field.value_types.includes("string") &&
      field.value_types.includes("boolean")
    )
      return (
        <label key={field.name}>
          {labelFor(field.name)}
          <select
            aria-label={labelFor(field.name)}
            required
            value={
              draft.fields[field.name] ??
              (typeof resolvedMetadata[field.name] === "boolean"
                ? String(resolvedMetadata[field.name])
                : "")
            }
            onChange={(e) => setField(field.name, e.target.value)}
          >
            <option value="">Choose a value</option>
            <option value="true">True</option>
            <option value="false">False</option>
          </select>
        </label>
      );
    if (
      !field.value_types.includes("string") &&
      (field.value_types.includes("integer") ||
        field.value_types.includes("number"))
    )
      return (
        <label key={field.name}>
          {labelFor(field.name)}
          <input
            aria-label={labelFor(field.name)}
            type="number"
            step={field.value_types.includes("integer") ? 1 : "any"}
            required
            value={
              draft.fields[field.name] ??
              (typeof resolvedMetadata[field.name] === "number"
                ? String(resolvedMetadata[field.name])
                : "")
            }
            min={typeof c.minimum === "number" ? c.minimum : undefined}
            max={typeof c.maximum === "number" ? c.maximum : undefined}
            onChange={(e) => setField(field.name, e.target.value)}
          />
        </label>
      );
    return (
      <label key={field.name}>
        {labelFor(field.name)}
        {Array.isArray(c.enum) ? (
          <select
            aria-label={labelFor(field.name)}
            value={
              draft.fields[field.name] ??
              (typeof resolvedMetadata[field.name] === "string"
                ? (resolvedMetadata[field.name] as string)
                : "")
            }
            required
            onChange={(e) => setField(field.name, e.target.value)}
          >
            <option value="">
              Choose {labelFor(field.name).toLowerCase()}
            </option>
            {c.enum.map((v) => (
              <option key={String(v)} value={String(v)}>
                {String(v)}
              </option>
            ))}
          </select>
        ) : (
          <input
            aria-label={labelFor(field.name)}
            value={
              draft.fields[field.name] ??
              (typeof resolvedMetadata[field.name] === "string"
                ? (resolvedMetadata[field.name] as string)
                : "")
            }
            required
            minLength={
              typeof c.minLength === "number" ? c.minLength : undefined
            }
            maxLength={
              typeof c.maxLength === "number" ? c.maxLength : undefined
            }
            onChange={(e) => setField(field.name, e.target.value)}
          />
        )}
        {field.description && (
          <span className="muted">{field.description}</span>
        )}
      </label>
    );
  };
  const simpleDocument =
    draft.kind === "document@1" &&
    required.length === 1 &&
    required[0].name === "title" &&
    !required[0].advanced;
  return (
    <section className="add-document-form">
      <form onSubmit={(e) => void submit(e)}>
        <fieldset disabled={busy}>
          <div className="input-mode">
            <button
              type="button"
              aria-pressed={draft.type === "text"}
              onClick={() => switchType("text")}
            >
              Pasted text
            </button>
            <button
              type="button"
              aria-pressed={draft.type === "bytes"}
              onClick={() => switchType("bytes")}
            >
              Files
            </button>
          </div>
          <div className="field-grid">
            {required.some((f) => f.name === "title") ? (
              formField(required.find((f) => f.name === "title")!)
            ) : (
              <label>
                Title
                <input
                  value={draft.fields.title ?? ""}
                  onChange={(e) => setField("title", e.target.value)}
                />
              </label>
            )}
            <label>
              Destination
              <select
                aria-label="Destination"
                value={draft.collection}
                required
                onChange={(e) => update({ collection: e.target.value })}
              >
                <option value="">Choose one collection</option>
                {active.map((c) => (
                  <option key={c.collection_id} value={c.collection_id}>
                    {c.title || c.slug}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <LoadState {...collections} retry={() => void collections.load()} />
          <More
            {...collections}
            onClick={() => void collections.more()}
            label="Load more destinations"
          />
          {draft.type === "text" ? (
            <label>
              Content
              <textarea
                aria-label="Content"
                rows={10}
                required
                value={draft.text}
                onChange={(e) => {
                  const patch: Partial<typeof draft> = { text: e.target.value };
                  if (!draft.fields.title && e.target.value.trim())
                    patch.fields = {
                      ...draft.fields,
                      title: e.target.value
                        .split("\n")[0]
                        .replace(/^#+\s*/, "")
                        .trim(),
                    };
                  update(patch);
                }}
              />
            </label>
          ) : draft.type === "bytes" ? (
            <>
              <label>
                Source file
                <input
                  type="file"
                  onChange={(e) => void readFile(e.target.files?.[0])}
                />
              </label>
              {draft.file && (
                <p>
                  Selected file: {draft.file.name} ·{" "}
                  {draft.file.size.toLocaleString("en-US")} bytes.
                </p>
              )}
            </>
          ) : (
            <label>
              Registered-root path
              <input
                aria-label="Registered-root path"
                required
                value={draft.path}
                onChange={(e) => update({ path: e.target.value })}
              />
              <span className="muted">
                The server validates and acquires paths within registered roots.
              </span>
            </label>
          )}
          {!simpleDocument &&
            required.filter((f) => f.name !== "title").map(formField)}
          <LoadState {...schema} retry={() => void schema.load()} />
          <details
            open={draft.advanced}
            onToggle={(e) => {
              const advanced = e.currentTarget.open;
              if (advanced !== draft.advanced)
                setDraft((previous) => ({ ...previous, advanced }));
            }}
          >
            <summary>Advanced source options</summary>
            <div className="field-grid">
              <label>
                Input type
                <select
                  aria-label="Input type"
                  value={draft.type}
                  onChange={(e) => switchType(e.target.value)}
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
                  value={draft.key}
                  onChange={(e) => update({ key: e.target.value })}
                  placeholder="Uses the title when empty"
                />
              </label>
              <label>
                Kind
                <input
                  aria-label="Kind"
                  list="source-kinds"
                  value={draft.kind}
                  required
                  onChange={(e) => update({ kind: e.target.value })}
                />
                <datalist id="source-kinds">
                  {kinds.pages
                    .flatMap((p) => p.definitions)
                    .map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.label}
                      </option>
                    ))}
                </datalist>
              </label>
              <label>
                Format
                <input
                  value={draft.format}
                  onChange={(e) => update({ format: e.target.value })}
                  placeholder={
                    resolvedFormat
                      ? `Automatic: ${resolvedFormat}`
                      : "Enter an explicit parser format"
                  }
                />
              </label>
            </div>
            <label>
              Metadata JSON
              <textarea
                aria-label="Metadata JSON"
                rows={4}
                spellCheck={false}
                value={draft.metadata}
                onChange={(e) => update({ metadata: e.target.value })}
              />
            </label>
            <p className="muted">
              The registered schema remains authoritative, including optional
              nested fields. No metadata values are inferred beyond the editable
              title.
            </p>
            {schemaValue && (
              <JsonDetails
                value={schemaValue}
                label="Registered metadata schema"
              />
            )}
            <More
              {...kinds}
              onClick={() => void kinds.more()}
              label="Load more kinds"
            />
          </details>
          <div className="provider-context">
            <p>Active profile: {settings.value?.profile ?? "Unknown"}</p>
            <p>
              Upload grant:{" "}
              {handshake?.scope.upload_providers.join(", ") || "None"}.
              Configuration is not upload consent.
            </p>
          </div>
          {settings.error !== null && (
            <ErrorNotice
              error={settings.error}
              retry={() => void settings.load()}
            />
          )}
          {(catalog.error !== null || !manager) && (
            <p className="muted">
              Model readiness is unavailable to this credential. The server
              validates supported processing; ask a workspace manager if setup
              is required.
            </p>
          )}
          {prerequisites.map((p, i) => (
            <p key={i}>
              {p.message}{" "}
              <RouteLink
                to={p.route}
                onClick={() =>
                  setDraft((previous) => ({
                    ...previous,
                    setupReturn: {
                      route: "/dashboard/documents/add",
                      step: "edit",
                      draftId,
                    },
                  }))
                }
              >
                Set up
              </RouteLink>
            </p>
          ))}
          {readiness.error !== null && (
            <ErrorNotice
              error={readiness.error}
              retry={() => void readiness.load()}
            />
          )}
          {fileBusy && <Status>Reading selected file…</Status>}
          {fileError !== null && <ErrorNotice error={fileError} />}
          {validation !== null &&
            (draft.text || draft.file || draft.advanced) && (
              <ErrorNotice error={validation} />
            )}
          {!isPdf && (
            <p className="muted">
              Encoded request:{" "}
              {encoded
                ? `${encoded.size.toLocaleString("en-US")} bytes`
                : "Unknown"}{" "}
              · 1 MiB limit.
            </p>
          )}
          {!isPdf && tooLarge && (
            <Status tone="warning">
              Encoded request exceeds the 1 MiB limit. Shorten the text, choose
              a smaller file, or use a registered-root path.
            </Status>
          )}
          {!isPdf && (draft.type !== "text" || draft.kind !== "document@1") && (
            <label className="collection-option">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              I confirm this source and processing destination.
            </label>
          )}
          {(isPdf && draft.file) || pendingPdf.current ? (
            <PdfIntake
              file={isPdf && draft.file ? draft.file : pendingPdf.current!.file}
              descriptor={
                !isPdf ||
                !draft.file ||
                validation ||
                !schemaValue ||
                !(draft.key.trim() || draft.fields.title?.trim()) ||
                !destination ||
                prerequisites.some((p) => p.blocking) ||
                !handshake?.scope.capabilities.includes("write")
                  ? null
                  : ({
                      source_key:
                        draft.key.trim() || draft.fields.title?.trim() || "",
                      collection_id: draft.collection,
                      kind: draft.kind,
                      metadata: resolvedMetadata as Record<string, Json>,
                      expected_size_bytes: draft.file.size,
                      format: "pdf",
                    } satisfies PdfIntakeDescriptor)
              }
              client={adminClient}
              scope={handshake?.scope ?? null}
              epoch={session.sessionKey}
              ownerRef={receiptOwner}
              pendingRef={pendingPdf}
              onAccepted={(receipt, owner) => {
                if (
                  receiptOwner.current &&
                  receiptOwner.current !== owner &&
                  receiptOwner.current !== pendingPdf.current?.owner
                )
                  receiptOwner.current.dispose();
                receiptOwner.current = owner;
                setDraft((previous) => ({
                  ...previous,
                  receipt,
                  setupReturn: null,
                }));
              }}
            />
          ) : null}
          {!isPdf && (
            <>
              <button className="primary" type="submit" disabled={!canSubmit}>
                {busy
                  ? "Submitting…"
                  : draft.type === "text" && draft.kind === "document@1"
                    ? "Index text"
                    : "Submit source"}
              </button>
            </>
          )}
        </fieldset>
      </form>
      {error !== null && <ErrorNotice error={error} />}
      {draft.receipt && (
        <Status tone="success">
          Indexing accepted ·{" "}
          <RouteLink
            to={`/dashboard/jobs/${encodeURIComponent(draft.receipt.id)}`}
          >
            Follow job
          </RouteLink>{" "}
          · <RouteLink to="/dashboard/documents">Open documents</RouteLink>
        </Status>
      )}
    </section>
  );
}
