import { useEffect, useRef, useState } from "react";
import type {
  Json,
  SettingsFieldChange,
  SettingsFieldView,
  SettingsPreview,
} from "../../api/contracts";
import { ApiError } from "../../api/client";
import { applySettings, previewSettings } from "../../api/administration";
import { useModelDraft } from "./draft";
import { useSession } from "../../session";
import { JsonDetails } from "../../components/JsonDetails";
import { ErrorNotice, Status } from "../../components/Status";
type Draft = { action: "set"; text: string } | { action: "inherit" };
function fieldLabel(path: string) {
  const labels: Record<string, string> = {
    offline: "Offline network policy",
    "index.text.backend": "Text index backend",
    "index.image.backend": "Image index backend",
    "extraction.parser": "Extraction parser",
  };
  return (
    labels[path] ||
    path
      .replace(/^providers\./, "")
      .replaceAll(".", " ")
      .replaceAll("_", " ")
      .replace(/\bref\b/g, "reference")
  );
}
function parse(
  field: SettingsFieldView["fields"][number],
  text: string,
): Exclude<Json, null> {
  const type = field.value_schema.type;
  const value =
    type === "boolean" ||
    type === "integer" ||
    type === "number" ||
    type === "array"
      ? JSON.parse(text)
      : text;
  if (value === null || (type === "integer" && !Number.isInteger(value)))
    throw new ApiError("INVALID_REQUEST", `Invalid value for ${field.path}.`);
  return value;
}
const groups = [
  [
    "Workspace network and index",
    (p: string) => p === "offline" || p.startsWith("index."),
  ],
  [
    "Extraction and OCR",
    (p: string) =>
      p.startsWith("extraction.") || p.startsWith("providers.vision."),
  ],
  ["Text embedding", (p: string) => p.startsWith("providers.text.")],
  ["Image embedding", (p: string) => p.startsWith("providers.image.")],
  ["Reranking", (p: string) => p.startsWith("providers.rerank.")],
  [
    "Advanced retrieval defaults",
    (p: string) => p.startsWith("retrieval.") || p.startsWith("limits."),
  ],
] as const;
export function GuidedSettings({
  fields,
  onActivated,
}: {
  fields: SettingsFieldView;
  onActivated: () => Promise<void>;
}) {
  const { setDraft: setModelDraft } = useModelDraft();
  const { adminClient: client } = useSession();
  const [draft, setDraft] = useState<Record<string, Draft>>({});
  const [preview, setPreview] = useState<SettingsPreview | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const lifetime = useRef(new AbortController());
  useEffect(() => {
    lifetime.current = new AbortController();
    return () => lifetime.current.abort();
  }, [client]);
  const change = (path: string, value: Draft) => {
    setDraft((d) => ({ ...d, [path]: value }));
    setPreview(null);
    setError(null);
  };
  const patch = () => {
    const changes = Object.entries(draft).map(
      ([path, d]): SettingsFieldChange =>
        d.action === "inherit"
          ? { path, action: "inherit" }
          : {
              path,
              action: "set",
              value: parse(fields.fields.find((f) => f.path === path)!, d.text),
            },
    );
    if (!changes.length || changes.length > 32)
      throw new ApiError(
        "INVALID_REQUEST",
        "Select between 1 and 32 distinct setting changes.",
      );
    return { base_workspace_sha256: fields.base_workspace_sha256, changes };
  };
  const run = async (activate: boolean) => {
    if (!client || busy || (activate && !preview?.activation_allowed)) return;
    const signal = lifetime.current.signal;
    setBusy(true);
    setError(null);
    try {
      if (activate) {
        await applySettings(client, patch(), signal);
        if (!signal.aborted) {
          setDraft({});
          setPreview(null);
          setModelDraft((d) => ({
            ...d,
            notice: "Configuration activated. A rebuild has not been queued.",
          }));
          await onActivated();
        }
      } else {
        const value = await previewSettings(client, patch(), signal);
        if (!signal.aborted) setPreview(value);
      }
    } catch (e) {
      if (!signal.aborted) setError(e);
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  };
  return (
    <section aria-label="Guided workspace settings">
      <h2>Draft and validate settings</h2>
      {fields.blockers.length > 0 && (
        <Status tone="warning">
          Current configuration constraints: {fields.blockers.join(", ")}
        </Status>
      )}
      <p>
        Entire workspace · references and configuration only. Environment
        credential values must be supplied to the server process. Preview never
        checks a live connection.
      </p>
      {groups.map(([title, matches]) => (
        <details
          key={title}
          open={
            title === "Workspace network and index" ||
            title === "Text embedding"
          }
        >
          <summary>{title}</summary>
          <div className="field-grid">
            {fields.fields
              .filter((f) => matches(f.path))
              .map((f) => {
                const d = draft[f.path];
                const text =
                  d?.action === "set"
                    ? d.text
                    : typeof f.effective_value === "string"
                      ? f.effective_value
                      : f.effective_value === null
                        ? ""
                        : JSON.stringify(f.effective_value);
                const choices = f.choices.length
                  ? f.choices
                  : f.value_schema.type === "boolean"
                    ? [true, false]
                    : [];
                return (
                  <div className="setting-field" key={f.path}>
                    <label>
                      {fieldLabel(f.path)}
                      {choices.length ? (
                        <select
                          aria-label={f.path}
                          disabled={busy || d?.action === "inherit"}
                          value={text}
                          onChange={(e) =>
                            change(f.path, {
                              action: "set",
                              text: e.target.value,
                            })
                          }
                        >
                          <option value="">Choose a value</option>
                          {choices.map((v) => (
                            <option key={String(v)} value={String(v)}>
                              {String(v)}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          aria-label={f.path}
                          disabled={busy || d?.action === "inherit"}
                          type={
                            f.value_schema.type === "integer" ||
                            f.value_schema.type === "number"
                              ? "number"
                              : "text"
                          }
                          value={text}
                          onChange={(e) =>
                            change(f.path, {
                              action: "set",
                              text: e.target.value,
                            })
                          }
                        />
                      )}
                    </label>
                    <label className="collection-option">
                      <input
                        type="checkbox"
                        checked={d?.action === "inherit"}
                        disabled={busy}
                        onChange={(e) => {
                          if (e.target.checked)
                            change(f.path, { action: "inherit" });
                          else {
                            setDraft((old) => {
                              const next = { ...old };
                              delete next[f.path];
                              return next;
                            });
                            setPreview(null);
                          }
                        }}
                      />
                      Inherit {f.path}
                    </label>
                    <small>
                      {f.overridden ? "Workspace override" : "Inherited value"}
                    </small>
                    <JsonDetails value={f.origin} label="Value origin" />
                  </div>
                );
              })}
          </div>
        </details>
      ))}
      <div className="detail-toolbar">
        <button
          disabled={busy || !Object.keys(draft).length}
          onClick={() => void run(false)}
        >
          Preview changes
        </button>
        <button
          className="primary"
          disabled={busy || !preview?.activation_allowed}
          onClick={() => void run(true)}
        >
          Activate reviewed changes
        </button>
      </div>
      {error !== null && <ErrorNotice error={error} />}
      {preview && (
        <>
          <Status tone={preview.activation_allowed ? "neutral" : "warning"}>
            {preview.activation_allowed
              ? "Preview permits activation; server preparation still validates prerequisites."
              : `Activation blocked: ${preview.blockers.join(", ")}`}
          </Status>
          {preview.blockers.includes("PROFILE_INCOMPATIBLE") && (
            <p>
              Retained indexes constrain defaults, network policy and
              generations. Configure a fresh workspace before indexing, or use a
              separately qualified publication path. Compatible-channel rebuild
              cannot migrate a model generation.
            </p>
          )}
          <JsonDetails
            value={preview.changes}
            label="Review effective changes"
          />
          <p>A rebuild has not been queued.</p>
        </>
      )}
    </section>
  );
}
