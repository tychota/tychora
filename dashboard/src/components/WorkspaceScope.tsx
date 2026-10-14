import { useSession } from "../session";
import { usePages } from "../hooks/useResource";
import type { CollectionPage } from "../api/contracts";
import { LoadState, More } from "./Operations";
export function WorkspaceScope() {
  const session = useSession();
  const data = usePages<CollectionPage>(
    "/v1/collections?include_archived=false",
    session.adminClient,
  );
  const active = data.pages
    .flatMap((p) => p.collections)
    .filter(
      (c) =>
        !c.archived_at &&
        session.handshake?.scope.collection_ids.includes(c.collection_id),
    );
  return (
    <div className="scope-context">
      <details className="scope-selector">
        <summary>
          Collection scope ·{" "}
          {session.selectedCollections.length
            ? active
                .filter((c) =>
                  session.selectedCollections.includes(c.collection_id),
                )
                .map((c) => c.title || "Untitled collection")
                .join(", ") || `${session.selectedCollections.length} selected`
            : "No active collection"}
        </summary>
        <fieldset>
          <legend>Read and query collections</legend>
          <p className="muted">
            Add documents requires one explicit destination.
          </p>
          {active.map((c) => (
            <label className="collection-option" key={c.collection_id}>
              <input
                type="checkbox"
                checked={session.selectedCollections.includes(c.collection_id)}
                onChange={(e) =>
                  session.selectCollections(
                    e.target.checked
                      ? [...session.selectedCollections, c.collection_id]
                      : session.selectedCollections.filter(
                          (id) => id !== c.collection_id,
                        ),
                  )
                }
              />
              <span>{c.title || c.slug || "Untitled collection"}</span>
            </label>
          ))}
          {active.length > 0 && !data.next && (
            <button
              type="button"
              onClick={() =>
                session.selectCollections(active.map((c) => c.collection_id))
              }
            >
              Select all granted active collections
            </button>
          )}
          {!data.busy && !active.length && (
            <p>
              No active collections are available. Create or reactivate a
              collection in Collections.
            </p>
          )}
          <LoadState {...data} retry={() => void data.load()} />
          <More
            {...data}
            onClick={() => void data.more()}
            label="Load more collections for selection"
          />
        </fieldset>
      </details>
    </div>
  );
}
