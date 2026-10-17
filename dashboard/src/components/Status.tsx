import type { ReactNode } from "react";
import { ApiError, publicError } from "../api/client";

export function Status({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "warning" | "success";
}) {
  return (
    <p className={`status status-${tone}`} role="status">
      {children}
    </p>
  );
}
export function ErrorNotice({
  error,
  retry,
  retryLabel = "Retry",
}: {
  error: unknown;
  retry?: () => void;
  retryLabel?: string;
}) {
  const value: ApiError = publicError(error);
  return (
    <div className="error-notice" role="alert">
      <p>
        <strong>{value.code}</strong> · {value.message}
      </p>
      {value.retryable && retry && (
        <button type="button" onClick={retry}>
          {retryLabel}
        </button>
      )}
    </div>
  );
}
