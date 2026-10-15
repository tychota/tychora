import { useEffect, useRef, useState } from "react";
import { useSession } from "../session";
import { useResource } from "../hooks/useResource";
import type {
  ModelCatalog,
  ModelReadiness,
  ModelAssetPlan,
  SettingsFieldView,
} from "../api/contracts";
import { setupPlan } from "../api/administration";
import {
  useAdministration,
  AdministrationResult,
} from "../components/Administration";
import { PageHeading, LoadState, RouteLink } from "../components/Operations";
import { JsonDetails } from "../components/JsonDetails";
import { ErrorNotice } from "../components/Status";
import { GuidedSettings } from "./models/GuidedSettings";
import { useModelDraft } from "./models/draft";
import { Status } from "../components/Status";
import { ExpertSettings } from "./models/ExpertSettings";
import { useAddDocumentDraft } from "./documents/draft";
export function Models() {
  const { draft: modelDraft, setDraft: setModelDraft } = useModelDraft();
  const session = useSession();
  const client = session.adminClient;
  const manage = session.handshake?.scope.capabilities.includes("manage");
  const catalog = useResource<ModelCatalog>(
    manage ? "/v1/models" : null,
    client,
  );
  const fields = useResource<SettingsFieldView>(
    manage ? "/v1/settings/fields" : null,
    client,
  );
  const [selections, setSelections] = useState<string[]>(["output-tokenizer"]);
  const readiness = useResource<ModelReadiness>(
    manage && selections.length
      ? `/v1/models/readiness?${new URLSearchParams(selections.map((s) => ["selection", s]))}`
      : null,
    client,
  );
  const { draft } = useAddDocumentDraft();
  const [plan, setPlan] = useState<ModelAssetPlan | null>(null);
  const [planError, setPlanError] = useState<unknown>(null);
  const [planning, setPlanning] = useState(false);
  const [bundle, setBundle] = useState("");
  const operation = useAdministration();
  const lifetime = useRef(new AbortController());
  useEffect(() => {
    lifetime.current = new AbortController();
    return () => lifetime.current.abort();
  }, [client]);
  const activated = async () => {
    await session.refreshGrant();
  };
  return (
    <>
      <PageHeading title="Models">
        <p>
          Entire workspace · extraction, embedding and reranking. Availability,
          configuration, credentials and collection consent are separate facts.
        </p>
      </PageHeading>
      {modelDraft.notice && <Status>{modelDraft.notice}</Status>}
      {!manage ? (
        <p>
          Model administration requires complete workspace management authority.
        </p>
      ) : (
        <>
          <LoadState {...catalog} retry={() => void catalog.load()} />
          <LoadState {...fields} retry={() => void fields.load()} />
          {catalog.value && (
            <section>
              <h2>Configured capabilities</h2>
              <p>
                Workspace network policy:{" "}
                {catalog.value.offline ? "Offline" : "Online permitted"}. A
                configured provider is not a tested connection. PDF OCR policy
                is selected per intake.
              </p>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Capability</th>
                      <th>Configured identity</th>
                      <th>Package</th>
                      <th>Credentials</th>
                      <th>Upload consent</th>
                    </tr>
                  </thead>
                  <tbody>
                    {catalog.value.providers.map((p) => (
                      <tr key={p.slot}>
                        <th>{p.slot}</th>
                        <td>
                          {p.configured_provider} /{" "}
                          {p.configured_model || "No model"}
                          <br />
                          {p.model_qualification}
                        </td>
                        <td>
                          {p.package_available ? "Present" : "Unavailable"}
                        </td>
                        <td>Not checked</td>
                        <td>
                          {p.upload_allowed_collection_ids.length
                            ? `${p.upload_allowed_collection_ids.length} collection permissions reported`
                            : "No collection upload permission"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <RouteLink to="/dashboard/collections">
                Edit collection upload consent
              </RouteLink>
            </section>
          )}
          {fields.value && (
            <GuidedSettings
              key={fields.value.base_workspace_sha256}
              fields={fields.value}
              onActivated={activated}
            />
          )}
          <section className="operation-panel">
            <h2>Local assets and readiness</h2>
            <p>
              Packages do not prove installed assets. Unknown bounded hash
              verification is nonblocking: the server fully verifies assets
              before processing. Historical receipts do not prove current
              readiness.
            </p>
            <fieldset disabled={planning || operation.busy}>
              <legend>Inspect up to four selections</legend>
              {catalog.value?.selections.map((s) => (
                <label className="collection-option" key={s}>
                  <input
                    type="checkbox"
                    checked={selections.includes(s)}
                    disabled={!selections.includes(s) && selections.length >= 4}
                    onChange={(e) => {
                      setSelections((old) =>
                        e.target.checked
                          ? [...old, s]
                          : old.filter((v) => v !== s),
                      );
                      setPlan(null);
                    }}
                  />
                  {s} ·{" "}
                  {catalog.value?.selection_capabilities.find(
                    (c) => c.selection === s,
                  )?.packages_available
                    ? "packages present"
                    : "packages unavailable"}
                </label>
              ))}
            </fieldset>
            <LoadState {...readiness} retry={() => void readiness.load()} />
            {readiness.value && (
              <>
                {readiness.value.assets
                  .filter((a) => a.state === "missing" || a.state === "corrupt")
                  .map((a) => (
                    <Status key={a.asset_id} tone="warning">
                      {a.selection} · {a.asset_id}: {a.state}. This known
                      failure blocks processing until corrected.
                    </Status>
                  ))}
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Selection</th>
                        <th>Current state</th>
                        <th>Check coverage</th>
                      </tr>
                    </thead>
                    <tbody>
                      {readiness.value.selections.map((s) => (
                        <tr key={s.selection}>
                          <td>{s.selection}</td>
                          <td>{s.state}</td>
                          <td>
                            {s.complete
                              ? "Checked"
                              : "Incomplete; server verification required"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <JsonDetails
                  value={readiness.value.assets}
                  label="Current asset checks and reasons"
                />
                <JsonDetails
                  value={readiness.value.receipts}
                  label="Historical setup receipts"
                />
              </>
            )}
            <button
              disabled={planning || !selections.length}
              onClick={async () => {
                if (!client) return;
                const signal = lifetime.current.signal;
                setPlanning(true);
                setPlanError(null);
                try {
                  const p = await setupPlan(client, selections, signal);
                  if (!signal.aborted) setPlan(p);
                } catch (e) {
                  if (!signal.aborted) setPlanError(e);
                } finally {
                  if (!signal.aborted) setPlanning(false);
                }
              }}
            >
              Review asset setup plan
            </button>
            {planError !== null && <ErrorNotice error={planError} />}
            {plan && (
              <>
                <p>
                  Selected plan: {plan.selection.join(", ")} ·{" "}
                  {plan.total_size_bytes} declared bytes. Execution verifies
                  existing cache assets or provisions from an explicit local
                  bundle; no automatic download.
                </p>
                <JsonDetails
                  value={plan}
                  label="Asset hashes, licenses and source notices"
                />
                <label>
                  Local bundle server path (optional)
                  <input
                    value={bundle}
                    onChange={(e) => setBundle(e.target.value)}
                  />
                </label>
                <button
                  disabled={operation.busy}
                  onClick={async () => {
                    const result = await operation.run({
                      operation: "setup",
                      assets: plan.selection,
                      offline: true,
                      ...(bundle ? { local_bundle: bundle } : {}),
                    });
                    if (result) {
                      setModelDraft((d) => ({ ...d, setupResult: result }));
                      await readiness.load();
                      await session.refreshGrant();
                    }
                  }}
                >
                  Verify and provision selected local assets
                </button>
              </>
            )}
            <AdministrationResult
              result={operation.result ?? modelDraft.setupResult}
              error={operation.error}
            />
          </section>
          <ExpertSettings onActivated={activated} />
        </>
      )}
      {draft.setupReturn && (
        <p>
          <RouteLink to={draft.setupReturn.route}>
            Return to Add documents
          </RouteLink>{" "}
          · draft preserved in memory; prerequisites are re-read on return.
        </p>
      )}
    </>
  );
}
