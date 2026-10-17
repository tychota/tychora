import { useEffect, useRef, useState, type FormEvent } from "react";
import { useSession } from "../session";
import { ApiError, publicError } from "../api/client";
import type {
  Discovery,
  QueryRequest,
  QueryResult,
  Segment,
  Settings,
  Json,
} from "../api/contracts";
import {
  EvidenceReader,
  OriginalInspection,
  type EvidenceSelectionRole,
} from "../components/EvidenceReader";
import { ReturnedPassage } from "../components/ReturnedPassage";
import { VisualPassage } from "../components/VisualPassage";
import { StoredChunks } from "../components/StoredChunks";
import { effectiveMode, reviewAdmission } from "./search/review";
import { parseQueryDraft, requestMode } from "./search/request";
import { RouteLink } from "../routing";
import { Relations } from "../components/Relations";
import { ErrorNotice, Status } from "../components/Status";
import { JsonDetails, downloadJson } from "../components/JsonDetails";

export function Search() {
  const { client, handshake } = useSession();
  const [question, setQuestion] = useState("");
  const [limit, setLimit] = useState(8);
  const [maxTokens, setMaxTokens] = useState(6000);
  const [mode, setMode] = useState("");
  const [structured, setStructured] = useState(false);
  const [queryDraft, setQueryDraft] = useState(
    '{\n  "input": { "type": "question", "text": "" }\n}',
  );
  const [submittedBody, setSubmittedBody] = useState<Record<
    string,
    Json
  > | null>(null);
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
    unitId: string | null;
    segment: Segment | null;
    selectionRole: EvidenceSelectionRole;
  } | null>(null);
  const [readerView, setReaderView] = useState<"passage" | "chunks" | "full">(
    "passage",
  );
  const [submittedMode, setSubmittedMode] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    if (!client) return;
    setResult(null);
    setSubmittedBody(null);
    setSubmittedMode(null);
    setSelection(null);
    setRelationSeed(null);
    setLoading(false);
    setError(null);
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
  const submit = async (event?: FormEvent, overrideMode?: string) => {
    event?.preventDefault();
    if (!client || (!overrideMode && !structured && !question.trim())) return;
    const selectedFacet = discovery.facets?.definitions.find(
      (value) => value.id === facet,
    );
    let typedFacetValue: string | number | boolean = facetValue;
    if (!structured && !overrideMode && selectedFacet && facetValue) {
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
    const simpleBody: QueryRequest = {
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
      ...(maxTokens !== 6000
        ? {
            budgets: {
              max_tokens: maxTokens,
              tokenizer_id: "cl100k_base",
              max_response_bytes: 1048576,
            },
          }
        : {}),
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
    let body: Record<string, Json>;
    try {
      const source =
        overrideMode && submittedBody
          ? submittedBody
          : parseQueryDraft(
              structured ? queryDraft : JSON.stringify(simpleBody),
            );
      body = overrideMode ? { ...source, mode: overrideMode } : source;
      // The override also stays within the ordinary HTTP request boundary.
      parseQueryDraft(JSON.stringify(body));
    } catch (error) {
      setError(publicError(error));
      return;
    }
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    setResult(null);
    setSelection(null);
    setRelationSeed(null);
    setReaderView("passage");
    setSubmittedBody(body);
    setSubmittedMode(requestMode(body, effectiveMode(discovery.settings)));
    try {
      if (typeof body.mode !== "string" && !effectiveMode(discovery.settings)) {
        const settings = await client.request<Settings>("/v1/settings", {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setSubmittedMode(requestMode(body, effectiveMode(settings)));
      }
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
    setReaderView("passage");
    setSelection({
      unitId: unitId ?? null,
      segment,
      selectionRole:
        unitId && segment.matched_unit_ids.includes(unitId)
          ? "matched"
          : "context",
    });
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
  const modeId = submittedMode ?? (mode || effectiveMode(discovery.settings));
  const admission = reviewAdmission(
    discovery.profiles?.definitions.find((d) => d.id === modeId),
    handshake?.scope,
  );
  const inclusionMode = discovery.profiles?.definitions.find(
    (d) => reviewAdmission(d, handshake?.scope) === "included",
  );
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
        {!structured && (
          <label className="question-label" htmlFor="question">
            Question
          </label>
        )}
        <div className="question-row">
          {structured ? (
            <p>Structured request · Search uses the QueryRequest JSON below.</p>
          ) : (
            <textarea
              id="question"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              rows={2}
              maxLength={12000}
              placeholder="What do you need to verify?"
              required
            />
          )}
          <button
            className="primary"
            type="submit"
            disabled={
              loading ||
              (!structured && !question.trim()) ||
              !handshake?.scope.capabilities.includes("read")
            }
          >
            {loading ? "Searching…" : "Search evidence"}
          </button>
        </div>
        <p className="review-query-state" role="status">
          {admission === "unknown"
            ? `Review policy is unavailable for ${modeId || "the configured default"}. Select a registered retrieval mode when available.`
            : `Review-required content: ${admission} by ${modeId}. Inclusion does not accept evidence or change citable status.`}
        </p>
        {admission === "excluded" && (
          <div className="status status-warning">
            <p>
              This mode excludes indexed review-required OCR. No
              excluded-document count is available.
            </p>
            {inclusionMode ? (
              <>
                <p>
                  Switching to {inclusionMode.label || inclusionMode.id} also
                  changes that mode’s retrieval policy; other query settings are
                  preserved.
                </p>
                <button
                  type="button"
                  disabled={
                    loading ||
                    (!submittedBody && !structured && !question.trim())
                  }
                  onClick={() => {
                    void submit(undefined, inclusionMode.id);
                  }}
                >
                  Include review-required content
                </button>
              </>
            ) : (
              <p>No authorized registered mode currently permits inclusion.</p>
            )}
          </div>
        )}
        <details className="search-options">
          <summary>Filters & retrieval options</summary>
          <label className="collection-option">
            <input
              type="checkbox"
              checked={structured}
              onChange={(event) => {
                setStructured(event.target.checked);
              }}
            />
            Structured request
          </label>
          {structured && (
            <div className="structured-query">
              <p>
                Edit the supported QueryRequest body. The workspace validates
                fields and permissions. Turn off structured request to return to
                your simple search draft.
              </p>
              <label htmlFor="query-request-json">QueryRequest JSON</label>
              <textarea
                id="query-request-json"
                value={queryDraft}
                onChange={(event) => setQueryDraft(event.target.value)}
                rows={12}
                spellCheck={false}
              />
            </div>
          )}
          {!structured && (
            <>
              <div className="filter-row">
                <label>
                  Response token budget
                  <input
                    type="number"
                    min={1}
                    max={6000}
                    required
                    value={maxTokens}
                    onChange={(e) => setMaxTokens(Number(e.target.value))}
                  />
                </label>
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
                  <select
                    aria-label="Retrieval mode"
                    value={mode}
                    onChange={(e) => setMode(e.target.value)}
                  >
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
                Dates apply to retained revision metadata. Missing dates follow
                your selected policy. Kind filters are available only when the
                registered facet schema supports them.
              </p>
            </>
          )}
        </details>
        <details>
          <summary>Configured providers and upload permission</summary>
          <div className="provider-context">
            <span>
              Profile:{" "}
              <strong>{discovery.settings?.profile ?? "Unknown"}</strong>
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
        </details>
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
                {result.segments.length > 0 &&
                  `${result.segments.length} ${result.segments.length === 1 ? "segment" : "segments"}`}
                {result.segments.length > 0 &&
                  result.visuals.length > 0 &&
                  " · "}
                {result.visuals.length > 0 &&
                  `${result.visuals.length} ${result.visuals.length === 1 ? "visual result" : "visual results"}`}
                {!result.segments.length &&
                  !result.visuals.length &&
                  "No returned evidence"}
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
          {result.segments.length === 0 && result.visuals.length === 0 && (
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
                      <>
                        <div
                          className="detail-toolbar"
                          aria-label="Evidence views"
                        >
                          <button
                            aria-pressed={readerView === "passage"}
                            onClick={() => setReaderView("passage")}
                          >
                            Returned passage
                          </button>
                          <button
                            aria-pressed={readerView === "chunks"}
                            onClick={() => setReaderView("chunks")}
                          >
                            Stored chunks
                          </button>
                          <button
                            aria-pressed={readerView === "full"}
                            disabled={!selection.unitId}
                            onClick={() => setReaderView("full")}
                          >
                            Full unit
                          </button>
                        </div>
                        <div className="unit-actions">
                          {[
                            ...selectedSegment.matched_unit_ids,
                            ...selectedSegment.context_only_unit_ids,
                          ].map((id, index) => (
                            <button
                              key={id}
                              title={id}
                              aria-label={`${selectedSegment.matched_unit_ids.includes(id) ? "Matched" : "Context"} · ${id}`}
                              aria-pressed={selection.unitId === id}
                              onClick={() => {
                                setRelationSeed(id);
                                setSelection({
                                  unitId: id,
                                  segment: selectedSegment,
                                  selectionRole:
                                    selectedSegment.matched_unit_ids.includes(
                                      id,
                                    )
                                      ? "matched"
                                      : "context",
                                });
                              }}
                            >
                              {selectedSegment.matched_unit_ids.includes(id)
                                ? "Matched"
                                : "Context"}{" "}
                              unit {index + 1}
                            </button>
                          ))}
                        </div>
                        {readerView === "passage" && (
                          <ReturnedPassage segment={selectedSegment} />
                        )}
                        {readerView === "chunks" && (
                          <StoredChunks
                            key={`${selectedSegment.segment_id}-${selection.unitId}`}
                            client={client}
                            segment={selectedSegment}
                            unitId={selection.unitId}
                          />
                        )}
                        <JsonDetails
                          value={selectedSegment}
                          label="Segment excerpts, anchors, omissions and records"
                        />
                      </>
                    )}
                    {selection.unitId &&
                      (readerView === "full" || !selectedSegment) && (
                        <EvidenceReader
                          key={selection.unitId}
                          client={client}
                          unitId={selection.unitId}
                          citation={selectedSegment?.citations.find(
                            (c) => c.unit_id === selection.unitId,
                          )}
                          sourceId={selectedSegment?.source_id}
                          revisionId={selectedSegment?.revision_id}
                          selectionRole={selection.selectionRole}
                        />
                      )}
                    {relationSeed && (
                      <p>
                        <RouteLink
                          to={`/dashboard/explore?unit=${encodeURIComponent(relationSeed)}`}
                        >
                          Open selected evidence in Explore
                        </RouteLink>
                      </p>
                    )}
                    {relationSeed && (
                      <Relations
                        key={`relations-${relationSeed}`}
                        client={client}
                        unitIds={[relationSeed]}
                        onSelectUnit={(unitId) =>
                          setSelection({
                            unitId,
                            segment: null,
                            selectionRole: "related",
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
              {result.visuals.map((visual, index) => (
                <article key={visual.asset_id}>
                  <h3>
                    {visual.caption ||
                      ("physical_page" in visual.locator &&
                      visual.locator.physical_page
                        ? `Page ${visual.locator.physical_page}`
                        : `Visual evidence ${index + 1}`)}
                  </h3>
                  <VisualPassage
                    key={visual.asset_id}
                    client={client}
                    visual={visual}
                  />
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
            {submittedBody && (
              <JsonDetails
                value={submittedBody}
                label="Submitted QueryRequest"
              />
            )}
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
