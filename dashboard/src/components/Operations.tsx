import type { ReactNode } from "react";
import { ErrorNotice, Status } from "./Status";
export { RouteLink } from "../routing";
export function PageHeading({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="page-heading">
      <h1>{title}</h1>
      {children}
    </header>
  );
}
export function LoadState({
  error,
  busy,
  retry,
}: {
  error: unknown;
  busy: boolean;
  retry: () => void;
}) {
  return (
    <>
      {busy && <Status>Loading scoped workspace data…</Status>}
      {error !== null && <ErrorNotice error={error} retry={retry} />}
    </>
  );
}
export function More({
  next,
  busy,
  onClick,
  label,
}: {
  next: string | null | undefined;
  busy: boolean;
  onClick: () => void;
  label: string;
}) {
  return next ? (
    <button type="button" disabled={busy} onClick={onClick}>
      {label}
    </button>
  ) : null;
}
