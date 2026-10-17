import { useSession } from "../session";
import { useResource } from "../hooks/useResource";
import type { Settings as SettingsValue } from "../api/contracts";
import { PageHeading, LoadState, RouteLink } from "../components/Operations";
import { JsonDetails } from "../components/JsonDetails";
import {
  Health,
  Rebuild,
  BackupRestore,
  Retention,
} from "./settings/WorkspaceOperations";
export function Settings() {
  const { adminClient, handshake } = useSession();
  const data = useResource<SettingsValue>("/v1/settings", adminClient);
  return (
    <>
      <PageHeading title="Settings">
        <p>
          Entire workspace · context, measured health and supported maintenance.
          The server verifies complete authority for workspace operations.
        </p>
      </PageHeading>
      <section>
        <h2>Workspace context</h2>
        <p>Workspace {handshake?.workspace_id}</p>
        <LoadState {...data} retry={() => void data.load()} />
        {data.value && (
          <>
            <p>Effective profile: {data.value.profile}</p>
            <JsonDetails
              value={data.value}
              label="Redacted effective configuration and origins"
            />
          </>
        )}
        <p>
          <RouteLink to="/dashboard/models">
            Edit model and processing configuration
          </RouteLink>
        </p>
        <p>
          <RouteLink to="/dashboard/collections">
            Edit collection upload consent
          </RouteLink>
        </p>
      </section>
      <Health />
      {handshake?.scope.capabilities.includes("manage") ? (
        <>
          <Rebuild />
          <BackupRestore />
          <Retention />
        </>
      ) : (
        <p>Workspace mutations require complete management authority.</p>
      )}
      <section className="operation-panel">
        <h2>Query history policy</h2>
        <p>
          Activity uses the exact selected query scope. Metadata-only history
          does not retain query text. History retention, expiry and trace purge
          controls are unavailable.
        </p>
      </section>
    </>
  );
}
