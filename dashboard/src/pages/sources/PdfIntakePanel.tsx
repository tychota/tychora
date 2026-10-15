import { useEffect, useRef, useState, type RefObject } from "react";
import { ApiClient, publicError } from "../../api/client";
import type {
  PdfIntakeDescriptor,
  PdfIntakeStatus,
  Scope,
} from "../../api/contracts";
import type { JobHandle } from "../../api/operations";
import { ErrorNotice, Status } from "../../components/Status";
import { JsonDetails } from "../../components/JsonDetails";
import { SubmissionIntent } from "./submission";
import {
  PDF_LIMIT,
  preparePdf,
  uploadPdf,
  analysePdf,
  inspectPdf,
  abortPdf,
  submitPdf,
  type PendingPdfSubmission,
} from "./pdfIntake";
interface Receipt {
  owner: ApiClient;
  status: PdfIntakeStatus;
  submitted: boolean;
  intent: SubmissionIntent;
}
export function PdfIntake({
  file,
  descriptor,
  client,
  scope,
  epoch,
  ownerRef,
  pendingRef,
  onAccepted,
}: {
  file: File;
  descriptor: PdfIntakeDescriptor | null;
  client: ApiClient | null;
  scope: Scope | null;
  epoch: number;
  ownerRef: RefObject<ApiClient | null>;
  pendingRef?: RefObject<PendingPdfSubmission | null>;
  onAccepted: (handle: JobHandle, owner: ApiClient) => void;
}) {
  const localPending = useRef<PendingPdfSubmission | null>(null);
  const pending = pendingRef ?? localPending;
  const [status, setStatus] = useState<PdfIntakeStatus | null>(null);
  const [phase, setPhase] = useState("selected");
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const [expired, setExpired] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const current = useRef<Receipt | null>(null);
  const operation = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const mounted = useRef(true);
  const cleaning = useRef(new Set<ApiClient>());
  const descriptorKey = JSON.stringify(descriptor);
  const abandon = (report: boolean) => {
    generation.current++;
    operation.current?.abort();
    operation.current = null;
    const old = current.current;
    current.current = null;
    if (!old || old.submitted || pending.current?.owner === old.owner) return;
    cleaning.current.add(old.owner);
    const cleanup = new AbortController();
    const timeout = setTimeout(() => cleanup.abort(), 5000);
    void abortPdf(old.owner, old.status.intake_id, cleanup.signal)
      .catch((e) => {
        if (
          report &&
          mounted.current &&
          generation.current === cleanupGeneration
        )
          setNotice(
            (previous) =>
              `${previous} Previous upload cleanup: ${publicError(e).message} Its expiration remains the fallback.`,
          );
      })
      .finally(() => {
        clearTimeout(timeout);
        cleaning.current.delete(old.owner);
        if (ownerRef.current === old.owner) ownerRef.current = null;
        old.owner.dispose();
      });
    const cleanupGeneration = generation.current;
  };
  useEffect(() => {
    mounted.current = true;
    const retained = pending.current;
    if (retained)
      current.current = {
        owner: retained.owner,
        status: retained.status,
        submitted: false,
        intent: new SubmissionIntent(),
      };
    setStatus(retained?.status ?? null);
    setPhase(retained ? "analysed" : "selected");
    setConfirmed(false);
    setError(null);
    setNotice(
      retained
        ? "A previous confirmed PDF submission is unresolved. Check its job or retry its original hashes before starting another upload."
        : "",
    );
    setExpired(false);
    setUncertain(!!retained);
    return () => {
      mounted.current = false;
      abandon(true);
    };
  }, [file, descriptorKey, epoch]);
  useEffect(() => {
    if (!status || status.state === "submitted") return;
    const check = () => setExpired(Date.now() >= Date.parse(status.expires_at));
    check();
    const timer = setTimeout(
      check,
      Math.min(
        2147483647,
        Math.max(0, Date.parse(status.expires_at) - Date.now()),
      ),
    );
    return () => clearTimeout(timer);
  }, [status]);
  const active = ["preparing", "uploading", "analysing", "submitting"].includes(
    phase,
  );
  const valid =
    !!descriptor &&
    !!client &&
    !!scope &&
    file.size > 0 &&
    file.size <= PDF_LIMIT;
  const analysis = status?.analysis;
  const ready =
    phase === "analysed" &&
    !!analysis?.proposed_policy &&
    !!analysis.policy_sha256 &&
    !analysis.blockers.length &&
    !expired &&
    Date.now() < Date.parse(status!.expires_at);
  const stop = () => {
    const analysing = phase === "analysing";
    abandon(true);
    setPhase("selected");
    setStatus(null);
    setConfirmed(false);
    setNotice(
      analysing
        ? "Stopped waiting. The bounded server analysis may still finish; receipt cleanup may remain busy until expiration."
        : "Upload stopped. The server releases interrupted upload staging; an unavailable receipt expires automatically.",
    );
  };
  const start = async () => {
    if (!valid || active || uncertain || pending.current) return;
    abandon(true);
    setStatus(null);
    setError(null);
    setNotice("");
    setConfirmed(false);
    setExpired(false);
    const version = generation.current;
    const controller = new AbortController();
    operation.current = controller;
    if (ownerRef.current && !cleaning.current.has(ownerRef.current))
      ownerRef.current.dispose();
    const owner = client.withGrant(scope).narrow([descriptor.collection_id]);
    ownerRef.current = owner;
    const live = () =>
      generation.current === version && !controller.signal.aborted;
    let stage = "Preparation";
    try {
      setPhase("preparing");
      const prepared = await preparePdf(owner, descriptor, controller.signal);
      if (!live()) {
        void abortPdf(owner, prepared.intake_id, new AbortController().signal)
          .catch(() => {})
          .finally(() => owner.dispose());
        return;
      }
      current.current = {
        owner,
        status: prepared,
        submitted: false,
        intent: new SubmissionIntent(),
      };
      setStatus(prepared);
      setPhase("uploading");
      stage = "Upload";
      const staged = await uploadPdf(
        owner,
        prepared.intake_id,
        file,
        controller.signal,
      );
      if (!live()) return;
      current.current.status = staged;
      setStatus(staged);
      setPhase("analysing");
      stage = "Analysis";
      const analysed = await analysePdf(
        owner,
        staged.intake_id,
        controller.signal,
      );
      if (!live()) return;
      current.current.status = analysed;
      setStatus(analysed);
      setPhase("analysed");
    } catch (e) {
      if (live()) {
        setError(publicError(e));
        setNotice(`${stage} failed. No indexing acceptance was received.`);
        setPhase("failed");
      }
    } finally {
      if (
        (!current.current || current.current.owner !== owner) &&
        !cleaning.current.has(owner)
      ) {
        if (ownerRef.current === owner) ownerRef.current = null;
        owner.dispose();
      }
    }
  };
  const submit = async () => {
    const receipt = current.current;
    if (!ready || !confirmed || !receipt || !analysis?.policy_sha256) return;
    const controller = new AbortController();
    operation.current = controller;
    const version = generation.current;
    const live = () =>
      generation.current === version && !controller.signal.aborted;
    const body = pending.current?.submission ?? {
      analysis_sha256: analysis.analysis_sha256,
      policy_sha256: analysis.policy_sha256,
    };
    const key =
      pending.current?.key ??
      receipt.intent.keyFor(JSON.stringify([receipt.status.intake_id, body]));
    if (!pending.current && descriptor)
      pending.current = {
        file,
        owner: receipt.owner,
        status: receipt.status,
        submission: body,
        key,
        descriptor,
      };
    const accept = (job: JobHandle) => {
      if (!live()) return;
      receipt.submitted = true;
      pending.current = null;
      setUncertain(false);
      setStatus({ ...receipt.status, state: "submitted", job });
      setPhase("submitted");
      setConfirmed(false);
      onAccepted(job, receipt.owner);
    };
    setPhase("submitting");
    setError(null);
    setNotice("");
    try {
      accept(
        await submitPdf(
          receipt.owner,
          receipt.status.intake_id,
          body,
          key,
          controller.signal,
        ),
      );
    } catch (e) {
      if (!live()) return;
      try {
        const recovered = await inspectPdf(
          receipt.owner,
          receipt.status.intake_id,
          controller.signal,
        );
        if (recovered.job) {
          accept(recovered.job);
          return;
        }
      } catch {
        /* Original authority only; preserve ambiguous intent for retry. */
      }
      if (live()) {
        setError(publicError(e));
        setPhase("analysed");
        if (
          ["SCOPE_CHANGED", "FORBIDDEN_SCOPE"].includes(publicError(e).code)
        ) {
          pending.current = null;
          setUncertain(false);
          setConfirmed(false);
          setExpired(true);
          setNotice(
            "This upload can no longer be submitted under its original scope and processing. Preserve the file and analyse again; unavailable cleanup expires automatically.",
          );
        } else if (
          [
            "CAPABILITY_MISSING",
            "INVALID_REQUEST",
            "LIMIT_EXCEEDED",
            "IDEMPOTENCY_CONFLICT",
            "INTEGRITY_ERROR",
          ].includes(publicError(e).code)
        ) {
          pending.current = null;
          setUncertain(false);
          setConfirmed(false);
          setNotice(
            "Ingestion was rejected before confirmed acceptance. Correct the reported problem and confirm again or analyse again.",
          );
        } else {
          setUncertain(true);
          setNotice(
            "Submission was not confirmed. Retry this same submission to recover any accepted job; do not start another upload while acceptance is uncertain.",
          );
        }
      }
    }
  };
  const checkSubmission = async () => {
    const retained = pending.current;
    if (!retained || active) return;
    const controller = new AbortController();
    operation.current = controller;
    const version = generation.current;
    const live = () =>
      generation.current === version &&
      !controller.signal.aborted &&
      operation.current === controller &&
      pending.current === retained;
    try {
      const inspected = await inspectPdf(
        retained.owner,
        retained.status.intake_id,
        controller.signal,
      );
      if (!live()) return;
      if (inspected.job) {
        pending.current = null;
        setUncertain(false);
        setStatus(inspected);
        setPhase("submitted");
        if (current.current) current.current.submitted = true;
        onAccepted(inspected.job, retained.owner);
      } else if (["expired", "aborted", "failed"].includes(inspected.state)) {
        pending.current = null;
        setUncertain(false);
        setStatus(inspected);
        setExpired(true);
        setNotice(
          "The previous upload has no accepted job and has ended. Analyse again.",
        );
      } else
        setNotice(
          "No accepted job is confirmed yet. Retry the original confirmed submission; a new upload remains disabled.",
        );
    } catch (e) {
      if (live()) {
        const failure = publicError(e);
        setError(failure);
        if (["FORBIDDEN_SCOPE", "SCOPE_CHANGED"].includes(failure.code)) {
          pending.current = null;
          setUncertain(false);
          setConfirmed(false);
          setExpired(true);
          setNotice(
            "Original submission recovery is no longer authorized. Its expiration remains the fallback. Preserve the file and analyse again under the current destination grant.",
          );
        }
      }
    }
  };
  const classification = analysis
    ? analysis.native_text_pages.length && analysis.image_only_pages.length
      ? "Mixed"
      : analysis.image_only_pages.length
        ? "Scanned / image-only"
        : analysis.native_text_pages.length
          ? "Native text"
          : analysis.unknown_pages.length
            ? "Unknown content"
            : "Blank"
    : "";
  return (
    <section aria-label="PDF intake" className="operation-panel">
      <h3>Analyse this PDF</h3>
      <p>
        {file.size.toLocaleString("en-US")} selected bytes · up to 100 MiB,
        subject to lower server limits.
      </p>
      <p>
        Upload and local analysis prepare this file for review. Indexing starts
        only after exact confirmation.
      </p>
      {!valid && (
        <Status tone="warning">
          Choose a valid destination and source metadata. PDF files must contain
          1–104,857,600 bytes.
        </Status>
      )}
      {!active && phase !== "submitted" && (
        <button
          type="button"
          disabled={!valid || uncertain}
          onClick={() => void start()}
        >
          {status ? "Analyse again" : "Analyse PDF"}
        </button>
      )}
      {active && (
        <>
          <Status>
            {phase === "uploading"
              ? "Uploading PDF…"
              : phase === "analysing"
                ? "Analysing all pages locally…"
                : phase === "submitting"
                  ? "Confirming durable job acceptance…"
                  : "Preparing PDF upload…"}
          </Status>
          {phase !== "submitting" && (
            <button type="button" onClick={stop}>
              {phase === "analysing" ? "Stop waiting" : "Cancel upload"}
            </button>
          )}
        </>
      )}
      {status && (
        <p>
          {status.received_bytes.toLocaleString("en-US")} server-confirmed
          received bytes · receipt expires{" "}
          {new Date(status.expires_at).toLocaleTimeString()}.
        </p>
      )}
      {analysis && (
        <>
          <h4>
            {classification} · {analysis.page_count} physical pages
          </h4>
          <p>
            {analysis.native_text_pages.length} native ·{" "}
            {analysis.image_only_pages.length} image-only ·{" "}
            {analysis.blank_pages.length} known blank ·{" "}
            {analysis.unknown_pages.length} unknown
          </p>
          <p>
            {!analysis.proposed_policy
              ? "No processing policy is currently available. Resolve the reported blockers and analyse again."
              : analysis.requires_ocr
                ? "This PDF requires local Docling OCR for the selected scanned pages. Transcription is derived, requires review and is not citable. Processing does not establish perfect transcription."
                : "The proposed policy processes native PDF text locally."}{" "}
            The default workspace profile stays unchanged.
          </p>
          {analysis.blockers.map((b, i) => (
            <Status key={i} tone="warning">
              {b}
            </Status>
          ))}
          <JsonDetails
            value={analysis.proposed_policy}
            label="Captured PDF processing policy"
          />
          {expired && (
            <Status tone="warning">
              Upload expired or its processing changed. Analyse again and
              confirm again.
            </Status>
          )}
          <label className="collection-option">
            <input
              type="checkbox"
              checked={confirmed}
              disabled={!ready}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            I confirm this exact analysis, processing policy and destination.
          </label>
          <button
            type="button"
            className="primary"
            disabled={!ready || !confirmed}
            onClick={() => void submit()}
          >
            {pending.current ? "Retry confirmed submission" : "Ingest PDF"}
          </button>
        </>
      )}
      {pending.current && (
        <>
          <p>
            Unresolved original submission:{" "}
            {pending.current.descriptor.source_key}. Its original destination
            and processing remain bound to the original upload.
          </p>
          <button
            type="button"
            disabled={active}
            onClick={() => void checkSubmission()}
          >
            Check submission
          </button>
          <JsonDetails
            value={pending.current.descriptor}
            label="Original confirmed destination and descriptor"
          />
        </>
      )}
      {notice && <Status>{notice}</Status>}
      {error !== null && <ErrorNotice error={error} />}
      {phase === "submitted" && (
        <Status>
          Durable job accepted. Queued or accepted does not mean indexed; follow
          the job for actual processing and publication.
        </Status>
      )}
    </section>
  );
}
