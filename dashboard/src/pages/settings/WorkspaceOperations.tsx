import { useRef, useState } from "react";
import {
  useAdministration,
  AdministrationResult,
} from "../../components/Administration";
export function Health() {
  const op = useAdministration();
  const [payloads, setPayloads] = useState(false);
  const [native, setNative] = useState(false);
  return (
    <section className="operation-panel">
      <h2>Measured health</h2>
      <p>Entire workspace · checks report measured recovery state.</p>
      <label className="collection-option">
        <input
          type="checkbox"
          checked={payloads}
          onChange={(e) => setPayloads(e.target.checked)}
        />
        Verify retained payload bytes
      </label>
      <label className="collection-option">
        <input
          type="checkbox"
          checked={native}
          onChange={(e) => setNative(e.target.checked)}
        />
        Verify native projections
      </label>
      <button
        disabled={op.busy}
        onClick={() =>
          void op.run({
            operation: "health",
            verify_payloads: payloads,
            verify_native: native,
            resume_pending_deletions: false,
          })
        }
      >
        Run health check
      </button>
      <AdministrationResult {...op} />
    </section>
  );
}
export function Rebuild() {
  const op = useAdministration();
  const [channels, setChannels] = useState<string[]>(["text"]);
  const [confirm, setConfirm] = useState(false);
  return (
    <section className="operation-panel">
      <h2>Recover compatible channels</h2>
      <p>
        Entire workspace · recover retained text/image/graph projections. This
        cannot migrate embedding, parser or model generations.
      </p>
      {["text", "image", "graph"].map((c) => (
        <label className="collection-option" key={c}>
          <input
            type="checkbox"
            checked={channels.includes(c)}
            onChange={(e) => {
              setChannels((old) =>
                e.target.checked ? [...old, c] : old.filter((v) => v !== c),
              );
              setConfirm(false);
            }}
          />
          {c}
        </label>
      ))}
      <label className="collection-option">
        <input
          type="checkbox"
          checked={confirm}
          onChange={(e) => setConfirm(e.target.checked)}
        />
        Confirm recovery of selected compatible channels
      </label>
      <button
        disabled={op.busy || !confirm || !channels.length}
        onClick={() =>
          void op.run({ operation: "rebuild", channels, revision_ids: [] })
        }
      >
        Recover selected channels
      </button>
      <AdministrationResult {...op} />
    </section>
  );
}
export function BackupRestore() {
  const op = useAdministration();
  const [backup, setBackup] = useState("");
  const [archive, setArchive] = useState("");
  const [destination, setDestination] = useState("");
  const [confirm, setConfirm] = useState(false);
  return (
    <section className="operation-panel">
      <h2>Backup and restore</h2>
      <p>
        Entire workspace · paths belong to the server filesystem. Restore
        creates a separate workspace in a clean destination; it does not switch
        this connection.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void op.run({
            operation: "export",
            destination: backup,
            include_originals: true,
            snapshot_ids: [],
          });
        }}
      >
        <label>
          Backup destination server path
          <input
            required
            value={backup}
            onChange={(e) => setBackup(e.target.value)}
          />
        </label>
        <p>
          Exports a complete verified backup including retained originals.
          Existing completed destination cannot be overwritten.
        </p>
        <button disabled={op.busy}>Export backup</button>
      </form>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (confirm)
            void op.run({
              operation: "import",
              archive_path: archive,
              destination,
            });
        }}
      >
        <label>
          Restore archive server path
          <input
            required
            value={archive}
            onChange={(e) => {
              setArchive(e.target.value);
              setConfirm(false);
            }}
          />
        </label>
        <label>
          Clean restore destination server path
          <input
            required
            value={destination}
            onChange={(e) => {
              setDestination(e.target.value);
              setConfirm(false);
            }}
          />
        </label>
        <label className="collection-option">
          <input
            type="checkbox"
            checked={confirm}
            onChange={(e) => setConfirm(e.target.checked)}
          />
          Confirm the archive and clean separate restore destination
        </label>
        <button disabled={op.busy || !confirm}>Restore verified backup</button>
      </form>
      <AdministrationResult {...op} />
    </section>
  );
}
export function Retention() {
  const targetVersion = useRef(0);
  const op = useAdministration();
  const [source, setSource] = useState("");
  const [revisions, setRevisions] = useState("");
  const [planned, setPlanned] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const reset = () => {
    targetVersion.current++;
    setPlanned(false);
    setConfirm(false);
    op.setResult(null);
  };
  const request = (confirmed: boolean) => ({
    operation: "purge-originals",
    source_id: source,
    revision_ids: revisions
      .split(",")
      .map((v) => v.trim())
      .filter(Boolean),
    confirm_original_removal: confirmed,
  });
  return (
    <details className="operation-panel">
      <summary>Advanced original retention</summary>
      <p>
        Remove selected original-byte bindings while retaining derived evidence.
        Review the actual reference cut; shared or leased bytes may remain.
      </p>
      <label>
        Purge source ID
        <input
          value={source}
          onChange={(e) => {
            setSource(e.target.value);
            reset();
          }}
        />
      </label>
      <label>
        Purge revision IDs
        <input
          value={revisions}
          onChange={(e) => {
            setRevisions(e.target.value);
            reset();
          }}
          placeholder="Comma-separated exact revision IDs"
        />
      </label>
      <button
        disabled={op.busy || !source || !revisions}
        onClick={async () => {
          const version = targetVersion.current;
          setConfirm(false);
          setPlanned(false);
          if (await op.run(request(false))) {
            if (version === targetVersion.current) setPlanned(true);
            else op.setResult(null);
          }
        }}
      >
        Plan original purge
      </button>
      <AdministrationResult {...op} />
      {planned && (
        <>
          <label className="collection-option">
            <input
              type="checkbox"
              checked={confirm}
              onChange={(e) => setConfirm(e.target.checked)}
            />
            I confirm removal of the planned original bindings
          </label>
          <button
            disabled={op.busy || !confirm}
            onClick={async () => {
              if (await op.run(request(true))) setPlanned(false);
            }}
          >
            Confirm original purge
          </button>
        </>
      )}
      <p>Trace/history purge and model migration are unavailable.</p>
    </details>
  );
}
