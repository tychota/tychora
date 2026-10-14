import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSession } from "../session";
import { usePages, useResource } from "../hooks/useResource";
import type { Discovery, Settings as SettingsValue } from "../api/contracts";
import { ApiError } from "../api/client";
import { encodedBody, REQUEST_LIMIT } from "./sources/submission";
import { PageHeading, LoadState, More } from "../components/Operations";
import { JsonDetails } from "../components/JsonDetails";
import { ErrorNotice, Status } from "../components/Status";
export function Settings() {
  const { client, handshake } = useSession();
  const data = useResource<SettingsValue>("/v1/settings");
  const kinds = usePages<Discovery>("/v1/discovery?subject=kinds");
  const capabilities = useResource<Discovery>(
    "/v1/discovery?subject=settings&limit=100",
  );
  const [profile, setProfile] = useState("");
  const [toml, setToml] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [activated, setActivated] = useState(false);
  const lifetime = useRef(new AbortController());
  useEffect(() => () => lifetime.current.abort(), []);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!client || busy) return;
    setError(null);
    setActivated(false);
    const encoded = encodedBody({ profile, workspace_toml: toml });
    if (encoded.size >= REQUEST_LIMIT) {
      setError(
        new ApiError(
          "INPUT_BUDGET_EXCEEDED",
          "Configuration request exceeds the 1 MiB limit.",
        ),
      );
      return;
    }
    setBusy(true);
    try {
      const result = await client.request<SettingsValue>("/v1/settings", {
        method: "PUT",
        body: encoded.body,
        signal: lifetime.current.signal,
      });
      if (!lifetime.current.signal.aborted) {
        data.setValue(result);
        setToml("");
        setActivated(true);
      }
    } catch (error) {
      if (!lifetime.current.signal.aborted) setError(error);
    } finally {
      if (!lifetime.current.signal.aborted) setBusy(false);
    }
  };
  return (
    <>
      <PageHeading title="Settings">
        <p>
          Effective workspace configuration. Credentials are redacted by the
          server.
        </p>
      </PageHeading>
      <LoadState {...data} retry={() => void data.load()} />
      {data.value && (
        <>
          <h2>Effective profile</h2>
          <p>{data.value.profile}</p>
          <h2>Providers</h2>
          {Object.entries(data.value.values.providers ?? {}).map(
            ([name, p]) => (
              <p key={name}>
                <strong>{name}</strong>: {p.provider}
                {p.model ? ` / ${p.model}` : ""}
                {p.enabled === false ? " · disabled" : ""}
              </p>
            ),
          )}
          <JsonDetails
            value={data.value.values}
            label="Redacted effective configuration"
          />
          <JsonDetails
            value={data.value.origins ?? {}}
            label="Configuration origins"
          />
        </>
      )}
      <section className="operation-panel">
        <h2>Kinds and capabilities</h2>
        <LoadState {...kinds} retry={() => void kinds.load()} />
        {kinds.pages
          .flatMap((p) => p.definitions)
          .map((kind, i) => (
            <article key={`${kind.id}-${i}`}>
              <h3>{kind.label || kind.id}</h3>
              <p>{kind.id}</p>
              <JsonDetails value={kind.definition} label="Kind definition" />
            </article>
          ))}
        <More
          {...kinds}
          onClick={() => void kinds.more()}
          label="Load more kinds"
        />
        <LoadState {...capabilities} retry={() => void capabilities.load()} />
        <JsonDetails
          value={capabilities.value}
          label="Installed capabilities and requirements"
        />
        <p>
          Granted capabilities:{" "}
          {handshake?.scope.capabilities.join(", ") || "None"}
        </p>
      </section>
      <section className="operation-panel">
        <h2>Query history policy</h2>
        <p>
          History retention, expiry and purge controls are unavailable.
          Metadata-only activity does not retain query text.
        </p>
      </section>
      {handshake?.scope.capabilities.includes("manage") ? (
        <section className="operation-panel">
          <h2>Activate configuration</h2>
          <p>
            Submit complete owner-supplied TOML and a registered workspace
            profile. Activation does not automatically queue a rebuild. Busy or
            incompatible changes may be rejected.
          </p>
          <form onSubmit={(e) => void submit(e)}>
            <fieldset disabled={busy}>
              <label>
                Activation profile
                <input
                  value={profile}
                  onChange={(e) => {
                    setProfile(e.target.value);
                    setActivated(false);
                  }}
                  required
                />
              </label>
              <label>
                Complete workspace TOML
                <textarea
                  value={toml}
                  onChange={(e) => {
                    setToml(e.target.value);
                    setActivated(false);
                  }}
                  required
                  rows={12}
                  spellCheck={false}
                />
              </label>
              <p className="muted">
                Use credential references in this draft. After successful
                activation the draft is cleared and only the server's redacted
                configuration is displayed.
              </p>
              <button type="submit" className="primary">
                {busy ? "Activating…" : "Activate configuration"}
              </button>
            </fieldset>
          </form>
          {error !== null && <ErrorNotice error={error} />}{" "}
          {activated && (
            <Status tone="success">
              Configuration activated. A rebuild has not been queued.
            </Status>
          )}
        </section>
      ) : (
        <p className="muted">
          Configuration activation requires manage capability.
        </p>
      )}
    </>
  );
}
