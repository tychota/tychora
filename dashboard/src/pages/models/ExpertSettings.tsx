import { useState } from "react";
import { useModelDraft } from "./draft";
import { useSession } from "../../session";
import { boundedBody } from "../../api/administration";
import { ErrorNotice } from "../../components/Status";
export function ExpertSettings({
  onActivated,
}: {
  onActivated: () => Promise<void>;
}) {
  const { adminClient } = useSession();
  const { draft, setDraft } = useModelDraft();
  const { profile, toml } = draft;
  const setProfile = (profile: string) => setDraft((d) => ({ ...d, profile }));
  const setToml = (toml: string) => setDraft((d) => ({ ...d, toml }));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  return (
    <details className="operation-panel">
      <summary>Expert complete TOML activation</summary>
      <p>
        Entire workspace. Use credential references. Busy or
        retained-incompatible activation is rejected; activation does not queue
        a rebuild.
      </p>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          if (!adminClient || busy) return;
          setBusy(true);
          setError(null);
          setDraft((d) => ({ ...d, notice: null }));
          try {
            await adminClient.request("/v1/settings", {
              method: "PUT",
              body: boundedBody({ profile, workspace_toml: toml }),
            });
            setToml("");
            setDraft((d) => ({
              ...d,
              notice: "Configuration activated. A rebuild has not been queued.",
            }));
            await onActivated();
          } catch (e) {
            setError(e);
          } finally {
            setBusy(false);
          }
        }}
      >
        <fieldset disabled={busy}>
          <label>
            Activation profile
            <input
              required
              value={profile}
              onChange={(e) => setProfile(e.target.value)}
            />
          </label>
          <label>
            Complete workspace TOML
            <textarea
              required
              rows={12}
              spellCheck={false}
              value={toml}
              onChange={(e) => setToml(e.target.value)}
            />
          </label>
          <button>Activate configuration</button>
        </fieldset>
      </form>
      {error !== null && <ErrorNotice error={error} />}
    </details>
  );
}
