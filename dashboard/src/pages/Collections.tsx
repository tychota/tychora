import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSession } from "../session";
import { usePages, useResource } from "../hooks/useResource";
import type {
  CollectionPage,
  CollectionSummary,
  CollectionConsentView,
} from "../api/contracts";
import { objectPath } from "../api/operations";
import {
  PageHeading,
  LoadState,
  More,
  RouteLink,
} from "../components/Operations";
import { JsonDetails } from "../components/JsonDetails";
import { ErrorNotice, Status } from "../components/Status";
import { useRoute } from "../routing";
import { useAddDocumentDraft } from "./documents/draft";
export function Collections({ id }: { id?: string }) {
  const session = useSession();
  const { navigate } = useRoute();
  const data = usePages<CollectionPage>(
    "/v1/collections?include_archived=true",
    session.adminClient,
  );
  const selected = useResource<CollectionSummary>(
    id ? `/v1/collections/${objectPath(id)}` : null,
    session.adminClient,
  );
  const [filter, setFilter] = useState("");
  const [creating, setCreating] = useState(false);
  const rows = data.pages.flatMap((p) => p.collections);
  const canManage = session.handshake?.scope.capabilities.includes("manage");
  return (
    <>
      <PageHeading title="Collections">
        <div className="heading-actions">
          <p>Organize documents with stable, named destinations.</p>
          {session.handshake?.managed_workspace_owner && canManage && (
            <button className="primary" onClick={() => setCreating((v) => !v)}>
              Create collection
            </button>
          )}
        </div>
      </PageHeading>
      {creating && (
        <CollectionEditor
          onDone={(created) => {
            setCreating(false);
            navigate(
              `/dashboard/collections/${objectPath(created.collection_id)}`,
            );
          }}
        />
      )}
      <div className="inventory-toolbar">
        <label>
          Find in loaded collections
          <input
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </label>
        <button onClick={() => void data.load()}>Refresh collections</button>
        <p className="muted">
          {rows.length} loaded{data.next ? " · More available" : ""}. Counts not
          reported remain unknown.
        </p>
      </div>
      <LoadState {...data} retry={() => void data.load()} />
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Lifecycle</th>
              <th>Documents</th>
              <th>Open</th>
            </tr>
          </thead>
          <tbody>
            {rows
              .filter((c) =>
                `${c.title} ${c.slug}`
                  .toLowerCase()
                  .includes(filter.toLowerCase()),
              )
              .map((c) => (
                <tr
                  key={c.collection_id}
                  aria-selected={c.collection_id === id}
                >
                  <td>
                    <RouteLink
                      to={`/dashboard/collections/${objectPath(c.collection_id)}`}
                    >
                      {c.title}
                    </RouteLink>
                    <span className="muted collection-slug">{c.slug}</span>
                  </td>
                  <td>{c.archived_at ? "Archived" : "Active"}</td>
                  <td>
                    {c.document_count === null ? "Unknown" : c.document_count}
                    {!c.summary_complete && " · partial summary"}
                  </td>
                  <td>
                    <button
                      disabled={!!c.archived_at}
                      onClick={() => {
                        session.selectCollections([c.collection_id]);
                        navigate("/dashboard/documents");
                      }}
                    >
                      Documents
                    </button>
                    <button
                      disabled={!!c.archived_at}
                      onClick={() => {
                        session.selectCollections([c.collection_id]);
                        navigate("/dashboard/search");
                      }}
                    >
                      Search
                    </button>
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      {!data.busy && data.pages.length > 0 && !rows.length && (
        <section className="empty-state">
          <h2>Create your first collection</h2>
          <p>
            A collection provides one named destination for new documents and a
            selectable reading scope.
          </p>
          {!session.handshake?.managed_workspace_owner && (
            <p>
              Ask the workspace owner to create a collection. This credential
              cannot expand its grant.
            </p>
          )}
        </section>
      )}
      <More
        {...data}
        onClick={() => void data.more()}
        label="Load more collections"
      />
      {id && (
        <>
          <LoadState {...selected} retry={() => void selected.load()} />
          {selected.value && (
            <CollectionEditor
              key={`${session.sessionKey}-${id}`}
              collection={selected.value}
              onDone={() => void data.load()}
            />
          )}
        </>
      )}
    </>
  );
}
function CollectionEditor({
  collection,
  onDone,
}: {
  collection?: CollectionSummary;
  onDone: (c: CollectionSummary) => void;
}) {
  const session = useSession();
  const { draft } = useAddDocumentDraft();
  const [title, setTitle] = useState(collection?.title ?? "");
  const [slug, setSlug] = useState(collection?.slug ?? "");
  const [defaultKind, setDefaultKind] = useState(
    collection?.default_kind_id ?? "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [confirmation, setConfirmation] = useState(false);
  const [receipt, setReceipt] = useState<unknown>(null);
  const lifetime = useRef(new AbortController());
  useEffect(() => () => lifetime.current.abort(), []);
  const consent = useResource<CollectionConsentView>(
    collection
      ? `/v1/collections/${objectPath(collection.collection_id)}/consent`
      : null,
    session.adminClient,
  );
  const manage = session.handshake?.scope.capabilities.includes("manage");
  const run = async (
    operation: () => Promise<CollectionSummary>,
    refresh = true,
  ) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await operation();
      if (lifetime.current.signal.aborted) return;
      if (refresh) await session.refreshGrant();
      onDone(result);
    } catch (e) {
      if (!lifetime.current.signal.aborted) setError(e);
    } finally {
      if (!lifetime.current.signal.aborted) setBusy(false);
    }
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!session.adminClient) return;
    void run(
      () =>
        collection
          ? session.adminClient!.editCollection(collection.collection_id, {
              title,
              slug,
              default_kind_id: defaultKind || null,
            })
          : session.createCollection({ title, slug }),
      !!collection,
    );
  };
  const upload = async (provider: string, action: string) => {
    if (!session.adminClient || !collection || busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await session.adminClient.request("/v1/jobs", {
        method: "POST",
        body: JSON.stringify({
          operation: "provider-upload-consent",
          collection_id: collection.collection_id,
          provider,
          action,
        }),
        signal: lifetime.current.signal,
      });
      if (!lifetime.current.signal.aborted) {
        setReceipt(result);
        await session.refreshGrant();
      }
    } catch (e) {
      if (!lifetime.current.signal.aborted) setError(e);
    } finally {
      if (!lifetime.current.signal.aborted) setBusy(false);
    }
  };
  return (
    <section
      className="collection-editor"
      aria-label={collection ? "Collection details" : "Create collection"}
    >
      <h2>{collection?.title ?? "New collection"}</h2>
      {manage ? (
        <form onSubmit={submit}>
          <fieldset disabled={busy}>
            <div className="field-grid">
              <label>
                Collection name
                <input
                  required
                  value={title}
                  onChange={(e) => {
                    setTitle(e.target.value);
                    if (!collection && !slug)
                      setSlug(
                        e.target.value
                          .toLowerCase()
                          .replace(/[^a-z0-9]+/g, "-")
                          .replace(/^-|-$/g, ""),
                      );
                  }}
                />
              </label>
              <label>
                Slug
                <input
                  required
                  value={slug}
                  disabled={collection?.slug === "default"}
                  pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
                  onChange={(e) => setSlug(e.target.value)}
                />
              </label>
            </div>
            {collection && (
              <label>
                Default kind (empty inherits the profile)
                <input
                  value={defaultKind}
                  onChange={(e) => setDefaultKind(e.target.value)}
                />
              </label>
            )}
            <button className="primary" type="submit">
              {busy ? "Saving…" : collection ? "Save collection" : "Create"}
            </button>
          </fieldset>
        </form>
      ) : (
        <p>Editing and lifecycle changes require manage capability.</p>
      )}
      {collection && (
        <>
          <p>
            {collection.archived_at ? "Archived" : "Active"} · Default kind:{" "}
            {collection.effective_default_kind_id ?? "Not configured"}
          </p>
          <LoadState {...consent} retry={() => void consent.load()} />
          {consent.value && (
            <details>
              <summary>Provider upload consent</summary>
              <p>
                Granting permits this collection's content to be sent to the
                named provider. Configuration and caller permission are separate
                requirements.
              </p>
              <p>
                Enabled providers:{" "}
                {consent.value.enabled_upload_providers.join(", ") || "None"}.
                Caller upload grant:{" "}
                {consent.value.caller_upload_providers.join(", ") || "None"}.
              </p>
              {manage &&
                ["voyage", "anthropic"].map((provider) => (
                  <button
                    key={provider}
                    disabled={busy}
                    onClick={() =>
                      void upload(
                        provider,
                        consent.value!.collection_upload_providers.includes(
                          provider,
                        )
                          ? "revoke"
                          : "grant",
                      )
                    }
                  >
                    {consent.value!.collection_upload_providers.includes(
                      provider,
                    )
                      ? "Revoke"
                      : "Grant"}{" "}
                    {provider} upload consent
                  </button>
                ))}
              <JsonDetails value={consent.value} label="Exact consent state" />
            </details>
          )}
          {manage &&
            (collection.archived_at ? (
              <button
                disabled={busy}
                onClick={() =>
                  void run(() =>
                    session.adminClient!.archiveCollection(
                      collection.collection_id,
                      false,
                    ),
                  )
                }
              >
                Reactivate collection
              </button>
            ) : (
              <>
                <button disabled={busy} onClick={() => setConfirmation(true)}>
                  Archive collection
                </button>
                {confirmation && (
                  <div role="group" aria-label="Confirm archive">
                    <p>
                      Archiving retains documents and evidence. Ordinary search
                      excludes this collection and new indexing is blocked.
                      Retained revisions remain inspectable where authorized.
                    </p>
                    <button
                      disabled={busy}
                      onClick={() =>
                        void run(() =>
                          session.adminClient!.archiveCollection(
                            collection.collection_id,
                            true,
                          ),
                        )
                      }
                    >
                      Confirm archive
                    </button>
                    <button onClick={() => setConfirmation(false)}>
                      Keep active
                    </button>
                  </div>
                )}
              </>
            ))}
          <JsonDetails
            value={collection}
            label="Collection identity and policy"
          />
        </>
      )}
      {error !== null && <ErrorNotice error={error} />}
      {receipt !== null && (
        <>
          <Status>
            Upload consent updated. Effective caller permission is supplied by
            the server.
          </Status>
          <JsonDetails value={receipt} label="Consent receipt" />
        </>
      )}
      {draft.setupReturn && (
        <RouteLink to={draft.setupReturn.route}>
          Return to Add documents
        </RouteLink>
      )}
    </section>
  );
}
