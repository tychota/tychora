import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSession } from "../session";
import { ApiError, publicError } from "../api/client";
import type {
  Discovery,
  QueryRequest,
  QueryResult,
  Segment,
  Settings,
} from "../api/contracts";
import {
  EvidenceReader,
  OriginalInspection,
} from "../components/EvidenceReader";
import { Relations } from "../components/Relations";
import { ErrorNotice, Status } from "../components/Status";
import { JsonDetails, downloadJson } from "../components/JsonDetails";

export function Search() {
  const { client, handshake } = useSession();
  const [question, setQuestion] = useState("");
  const [limit, setLimit] = useState(8);
  const [mode, setMode] = useState("");
  const [revisionSelector, setRevisionSelector] = useState<"current" | "all">(
    "current",
  );
  const [revisionIds, setRevisionIds] = useState("");
  const [asOf, setAsOf] = useState("");
  const [cutoff, setCutoff] = useState("");
  const [unknownDates, setUnknownDates] = useState<"strict" | "permissive">(
    "strict",
  );
  const [facet, setFacet] = useState("");
  const [facetValue, setFacetValue] = useState("");
  const [discovery, setDiscovery] = useState<{
    kinds?: Discovery;
    profiles?: Discovery;
    facets?: Discovery;
    settings?: Settings;
  }>({});
  const [discoveryError, setDiscoveryError] = useState<unknown>(null);
  const [result, setResult] = useState<QueryResult | null>(null);
  const [relationSeed, setRelationSeed] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [selection, setSelection] = useState<{
    unitId: string;
    segment: Segment | null;
    contextOnly: boolean;
  } | null>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    if (!client) return;
    const controller = new AbortController();
    void Promise.allSettled([
      client.request<Discovery>("/v1/discovery?subject=kinds&limit=100", {
        signal: controller.signal,
      }),
      client.request<Discovery>("/v1/discovery?subject=profiles&limit=100", {
        signal: controller.signal,
      }),
      client.request<Discovery>("/v1/discovery?subject=facets&limit=100", {
        signal: controller.signal,
      }),
      client.request<Settings>("/v1/settings", { signal: controller.signal }),
    ]).then((values) => {
      if (controller.signal.aborted) return;
      const [kinds, profiles, facets, settings] = values;
      setDiscovery({
        kinds: kinds.status === "fulfilled" ? kinds.value : undefined,
        profiles: profiles.status === "fulfilled" ? profiles.value : undefined,
        facets: facets.status === "fulfilled" ? facets.value : undefined,
        settings: settings.status === "fulfilled" ? settings.value : undefined,
      });
      const failure = values.find((value) => value.status === "rejected");
      if (failure?.status === "rejected")
        setDiscoveryError(publicError(failure.reason));
    });
    return () => {
      controller.abort();
      request.current?.abort();
    };
  }, [client]);
  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!client || !question.trim()) return;
    const selectedFacet = discovery.facets?.definitions.find(
      (value) => value.id === facet,
    );
    let typedFacetValue: string | number | boolean = facetValue;
    if (selectedFacet && facetValue) {
      const type = selectedFacet.definition.value_type;
      if (type === "integer" || type === "number") {
        typedFacetValue = Number(facetValue);
        if (
          !Number.isFinite(typedFacetValue) ||
          (type === "integer" && !Number.isInteger(typedFacetValue))
        ) {
          setError(
            new ApiError(
              "INVALID_REQUEST",
              "Enter a valid numeric facet value.",
            ),
          );
          return;
        }
      } else if (type === "boolean") typedFacetValue = facetValue === "true";
    }
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    setResult(null);
    setSelection(null);
    setRelationSeed(null);
    const body: QueryRequest = {
      input: { type: "question", text: question.trim() },
      limit: Math.max(1, Math.min(50, limit)),
      revision_selector: revisionSelector,
      revision_ids: revisionIds
        .split(",")
        .map((id) => id.trim())
        .filter(Boolean)
        .slice(0, 20),
      unknown_dates: unknownDates,
      trace: true,
      ...(mode ? { mode } : {}),
      ...(asOf ? { as_of: asOf } : {}),
      ...(cutoff ? { cutoff } : {}),
      ...(facet && facetValue
        ? {
            filters: {
              op: "eq" as const,
              field: facet,
              value: typedFacetValue,
            },
          }
        : {}),
    };
    try {
      const value = await client.request<QueryResult>("/v1/queries", {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify(body),
      });
      if (!controller.signal.aborted) setResult(value);
    } catch (error) {
      if (!controller.signal.aborted) setError(publicError(error));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  };
  const loadKinds = async () => {
    if (!client || !discovery.kinds?.next_cursor) return;
    try {
      const page = await client.request<Discovery>(
        `/v1/discovery?subject=kinds&limit=100&cursor=${encodeURIComponent(discovery.kinds.next_cursor)}`,
      );
      setDiscovery((previous) => ({
        ...previous,
        kinds: {
          ...page,
          definitions: [...previous.kinds!.definitions, ...page.definitions],
          counts: [...previous.kinds!.counts, ...page.counts],
        },
      }));
    } catch (error) {
      setDiscoveryError(publicError(error));
    }
  };
  const selectSegment = (segment: Segment) => {
    const unitId =
      segment.matched_unit_ids[0] ?? segment.context_only_unit_ids[0];
    setRelationSeed(unitId ?? null);
    setSelection(
      unitId
        ? {
            unitId,
            segment,
            contextOnly: !segment.matched_unit_ids.includes(unitId),
          }
        : null,
    );
  };
  const kinds = discovery.kinds?.definitions ?? [];
  const facets =
    discovery.facets?.definitions.filter(
      (value) =>
        typeof value.definition.source_binding === "string" &&
        Array.isArray(value.definition.operators) &&
        value.definition.operators.includes("eq"),
    ) ?? [];
  const facetType = facets.find((value) => value.id === facet)?.definition
    .value_type;
  const selectedSegment = selection?.segment;
  if (!client) return null;
  return (
    <>
      <header className="page-heading">
        <p className="eyebrow">Retrieve & inspect</p>
        <h1>Search</h1>
        <p className="intro">
          Find source evidence. Follow the exact passage back to its origin.
        </p>
      </header>
      <form className="search-form" onSubmit={(event) => void submit(event)}>
        <label className="question-label" htmlFor="question">
          Question
        </label>
        <div className="question-row">
          <textarea
            id="question"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            rows={2}
            maxLength={12000}
            placeholder="What do you need to verify?"
            required
          />
          <button
            className="primary"
            type="submit"
            disabled={
              loading ||
              !question.trim() ||
              !handshake?.scope.capabilities.includes("read")
            }
          >
            {loading ? "Searching…" : "Search evidence"}
          </button>
        </div>
        <details className="search-options">
          <summary>Filters & retrieval options</summary>
          <div className="filter-row">
            <label>
              Result limit
              <input
                type="number"
                min={1}
                max={50}
                value={limit}
                required
                onChange={(e) => setLimit(Number(e.target.value))}
              />
            </label>
            <label>
              Retrieval mode
              <select value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="">Configured default</option>
                {discovery.profiles?.definitions.map((definition) => (
                  <option key={definition.id} value={definition.id}>
                    {definition.label || definition.id}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Revisions
              <select
                value={revisionSelector}
                onChange={(e) =>
                  setRevisionSelector(e.target.value as "current" | "all")
                }
              >
                <option value="current">Current</option>
                <option value="all">All readable revisions</option>
              </select>
            </label>
            <label>
              As of date
              <input
                type="date"
                value={asOf}
                onChange={(e) => setAsOf(e.target.value)}
              />
            </label>
            <label>
              Cutoff date
              <input
                type="date"
                value={cutoff}
                onChange={(e) => setCutoff(e.target.value)}
              />
            </label>
            <label>
              Unknown dates
              <select
                value={unknownDates}
                onChange={(e) =>
                  setUnknownDates(e.target.value as "strict" | "permissive")
                }
              >
                <option value="strict">Strict</option>
                <option value="permissive">Permissive</option>
              </select>
            </label>
            <label className="wide-field">
              Exact revision IDs
              <input
                value={revisionIds}
                onChange={(e) => setRevisionIds(e.target.value)}
                placeholder="Comma-separated IDs; up to 20"
                maxLength={2600}
              />
            </label>
            {facets.length > 0 && (
              <>
                <label>
                  Metadata facet
                  <select
                    value={facet}
                    onChange={(e) => {
                      setFacet(e.target.value);
                      setFacetValue("");
                    }}
                  >
                    <option value="">No metadata filter</option>
                    {facets.map((definition) => (
                      <option key={definition.id} value={definition.id}>
                        {definition.label || definition.id}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Exact facet value
                  {facetType === "boolean" ? (
                    <select
                      value={facetValue}
                      onChange={(e) => setFacetValue(e.target.value)}
                    >
                      <option value="">No exact value</option>
                      <option value="true">True</option>
                      <option value="false">False</option>
                    </select>
                  ) : (
                    <input
                      type={
                        facetType === "integer" || facetType === "number"
                          ? "number"
                          : facetType === "date"
                            ? "date"
                            : "text"
                      }
                      step={facetType === "integer" ? 1 : "any"}
                      value={facetValue}
                      onChange={(e) => setFacetValue(e.target.value)}
                      disabled={!facet}
                      maxLength={500}
                    />
                  )}
                </label>
              </>
            )}
          </div>
          <p className="muted">
            Dates apply to retained revision metadata. Missing dates follow your
            selected policy. Kind filters are available only when the registered
            facet schema supports them.
          </p>
        </details>
        <div className="provider-context">
          <span>
            Profile: <strong>{discovery.settings?.profile ?? "Unknown"}</strong>
          </span>
          {discovery.settings?.values.providers &&
            Object.entries(discovery.settings.values.providers).map(
              ([name, provider]) => (
                <span key={name}>
                  {name}: <strong>{provider.provider}</strong>
                  {provider.model ? ` / ${provider.model}` : ""}
                </span>
              ),
            )}
          <span>
            Upload grant:{" "}
            {handshake?.scope.upload_providers.join(", ") || "None"}
          </span>
        </div>
        <details className="kind-discovery">
          <summary>
            Available kinds {kinds.length > 0 && `(${kinds.length})`}
          </summary>
          <ul>
            {kinds.map((definition) => (
              <li key={definition.id}>
                <strong>{definition.label || definition.id}</strong>
                <span>{definition.id}</span>
                <span>
                  {discovery.kinds?.counts.find(
                    (count) => count.name === definition.id,
                  )?.count ?? "Unknown"}{" "}
                  sources
                </span>
              </li>
            ))}
          </ul>
          {discovery.kinds?.next_cursor && (
            <button type="button" onClick={() => void loadKinds()}>
              Load more kinds
            </button>
          )}
          <p className="muted">
            Counts describe authorized sources. A kind with zero sources remains
            a valid registered kind.
          </p>
        </details>
        {discoveryError !== null && <ErrorNotice error={discoveryError} />}
      </form>
      {error !== null && (
        <ErrorNotice
          error={error}
          retry={() => void submit()}
          retryLabel="Retry query"
        />
      )}
      {loading && <Status>Retrieving scoped evidence…</Status>}
      {result ? (
        <>
          <div className="result-toolbar">
            <div>
              <strong>
                {result.segments.length}{" "}
                {result.segments.length === 1 ? "segment" : "segments"}
              </strong>
              <span className={`coverage coverage-${result.coverage.status}`}>
                Coverage: {result.coverage.status}
              </span>
            </div>
            <button
              type="button"
              onClick={() =>
                downloadJson(result, `query-${result.run_id}.json`)
              }
            >
              Export response JSON
            </button>
          </div>
          <div className="coverage-detail">
            <p>
              Completed channels:{" "}
              {result.coverage.completed_channels.join(", ") || "None"} ·
              Unavailable channels:{" "}
              {result.coverage.unavailable_channels.join(", ") || "None"}
            </p>
            {[...result.warnings, ...result.coverage.warnings].map(
              (warning, index) => (
                <Status key={index} tone="warning">
                  {warning}
                </Status>
              ),
            )}
            {result.coverage_gaps.map((gap, index) => (
              <Status key={index} tone="warning">
                {gap.code}: {gap.message}
                {gap.channel && ` (${gap.channel})`}
              </Status>
            ))}
            {result.decision_requirements.map((requirement) => (
              <Status key={requirement.requirement_id} tone="warning">
                Decision required: {requirement.reason}
              </Status>
            ))}
            <p className="muted">
              {result.trace === null
                ? "Trace details are unavailable for this query."
                : "Trace details are included in the response JSON."}
            </p>
          </div>
          {result.segments.length === 0 && (
            <div className="empty-state">
              <h2>No matching evidence</h2>
              <p>
                Try a more specific question, widen the date or revision
                filters, or select another granted collection.
              </p>
              {result.coverage.status !== "complete" && (
                <p>
                  Coverage is {result.coverage.status}; these results may not
                  include all relevant evidence.
                </p>
              )}
            </div>
          )}
          {result.segments.length > 0 && (
            <div className="search-workspace">
              <section className="result-list" aria-label="Search results">
                <ol>
                  {result.segments.map((segment, index) => (
                    <li key={segment.segment_id}>
                      <button
                        type="button"
                        className={
                          selectedSegment?.segment_id === segment.segment_id
                            ? "result-row selected"
                            : "result-row"
                        }
                        aria-pressed={
                          selectedSegment?.segment_id === segment.segment_id
                        }
                        onClick={() => selectSegment(segment)}
                      >
                        <span className="row-kicker">
                          <span>{String(index + 1).padStart(2, "0")}</span>
                          <span>
                            {kinds.find((kind) => kind.id === segment.kind)
                              ?.label ?? segment.kind}
                          </span>
                          <span>{segment.signals.join(" · ")}</span>
                        </span>
                        <span className="result-excerpt">
                          {segment.text ||
                            "Structured evidence; select to inspect records."}
                        </span>
                        <span className="result-source">
                          {segment.citations[0]?.source_ref ??
                            segment.source_id}
                        </span>
                        <span className="muted">
                          {segment.matched_unit_ids.length} matched ·{" "}
                          {segment.context_only_unit_ids.length} context
                          {segment.truncated ? " · truncated" : ""}
                          {!segment.complete ? " · incomplete" : ""}
                        </span>
                      </button>
                    </li>
                  ))}
                </ol>
              </section>
              <div className="reader-surface">
                {selection ? (
                  <>
                    {selectedSegment && (
                      <div className="segment-context">
                        <p className="eyebrow">
                          {selectedSegment.kind} · {selectedSegment.role} ·{" "}
                          {selectedSegment.lane}
                        </p>
                        <div className="unit-actions">
                          {[
                            ...selectedSegment.matched_unit_ids,
                            ...selectedSegment.context_only_unit_ids,
                          ].map((id) => (
                            <button
                              type="button"
                              key={id}
                              className={
                                selection.unitId === id ? "selected" : ""
                              }
                              aria-pressed={selection.unitId === id}
                              onClick={() => {
                                setRelationSeed(id);
                                setSelection({
                                  unitId: id,
                                  segment: selectedSegment,
                                  contextOnly:
                                    selectedSegment.context_only_unit_ids.includes(
                                      id,
                                    ),
                                });
                              }}
                            >
                              {selectedSegment.matched_unit_ids.includes(id)
                                ? "Matched"
                                : "Context"}{" "}
                              · {id}
                            </button>
                          ))}
                        </div>
                        {selectedSegment.context_excerpts.map(
                          (excerpt, index) => (
                            <details key={index}>
                              <summary>
                                Context excerpt · {excerpt.unit_id} ·{" "}
                                {excerpt.char_start}–{excerpt.char_end}
                              </summary>
                              <p className="evidence-text">{excerpt.text}</p>
                            </details>
                          ),
                        )}
                        <JsonDetails
                          value={selectedSegment}
                          label="Segment excerpts, anchors, omissions and records"
                        />
                      </div>
                    )}
                    <EvidenceReader
                      key={selection.unitId}
                      client={client}
                      unitId={selection.unitId}
                      citation={selectedSegment?.citations.find(
                        (citation) => citation.unit_id === selection.unitId,
                      )}
                      sourceId={selectedSegment?.source_id}
                      revisionId={selectedSegment?.revision_id}
                      contextOnly={selection.contextOnly}
                    />
                    {relationSeed && (
                      <Relations
                        key={`relations-${relationSeed}`}
                        client={client}
                        unitIds={[relationSeed]}
                        onSelectUnit={(unitId) =>
                          setSelection({
                            unitId,
                            segment: null,
                            contextOnly: false,
                          })
                        }
                      />
                    )}
                  </>
                ) : (
                  <div className="reader-empty">
                    <p className="eyebrow">Evidence reader</p>
                    <h2>Select a passage</h2>
                    <p>
                      Inspect exact evidence, provenance, retained originals and
                      bounded relations.
                    </p>
                  </div>
                )}
              </div>
            </div>
          )}
          {result.visuals.length > 0 && (
            <section className="visual-results">
              <h2>Visual evidence</h2>
              {result.visuals.map((visual) => (
                <article key={visual.asset_id}>
                  <h3>{visual.caption ?? visual.asset_id}</h3>
                  <OriginalInspection
                    client={client}
                    assetId={visual.asset_id}
                  />
                  <JsonDetails
                    value={visual}
                    label="Visual provenance and derived proxies"
                  />
                </article>
              ))}
            </section>
          )}
          <details className="query-metadata">
            <summary>Query identity & complete response</summary>
            <dl className="identity">
              <dt>Run</dt>
              <dd>{result.run_id}</dd>
              <dt>Snapshot</dt>
              <dd>{result.snapshot_id}</dd>
              <dt>Relation checks</dt>
              <dd>
                {result.checks.status}
                {result.checks.truncated ? " · truncated" : ""}
              </dd>
            </dl>
            <JsonDetails value={result} label="Authoritative query JSON" />
          </details>
        </>
      ) : (
        !loading &&
        error === null && (
          <div className="search-empty">
            <p className="eyebrow">Your workspace, cited precisely</p>
            <h2>Start with a question.</h2>
            <p>
              Results retain source, revision and evidence identities. Coverage
              tells you which retrieval channels contributed.
            </p>
          </div>
        )
      )}
    </>
  );
}
