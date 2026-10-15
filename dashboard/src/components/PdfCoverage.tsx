import type { Json, PdfCoverage as Coverage } from "../api/contracts";
/** Optional selected diagnostics, never inferred from bulk inventory or job success. */
export function PdfCoverage({ value }: { value: Json | undefined }) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const keys = [
    "native_pages",
    "ocr_pages",
    "blank_pages",
    "no_text_detected_pages",
  ] as const;
  if (
    typeof value.page_count !== "number" ||
    !keys.every(
      (k) =>
        Array.isArray(value[k]) && value[k].every((p) => typeof p === "number"),
    )
  )
    return null;
  const c = value as unknown as Coverage;
  return (
    <section aria-label="PDF processing coverage">
      <h4>PDF processing coverage</h4>
      <p>
        {c.page_count} physical pages · {c.native_pages.length} native ·{" "}
        {c.ocr_pages.length} OCR processed · {c.blank_pages.length} blank
      </p>
      <p>
        {c.no_text_detected_pages.length} OCR pages with no text detected. This
        is detector-relative, not a blank-page assertion.
      </p>
      {c.ocr_pages.length > 0 && (
        <p>
          OCR is derived, needs review and is not citable. Processing coverage
          does not establish complete transcription.
        </p>
      )}
    </section>
  );
}
