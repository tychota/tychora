import type { ReactNode } from "react";
import { useRoute } from "../App";
import { ErrorNotice, Status } from "./Status";
export function RouteLink({
  to,
  children,
}: {
  to: string;
  children: ReactNode;
}) {
  const { navigate } = useRoute();
  return (
    <a
      href={to}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}
export function PageHeading({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="page-heading">
      <p className="eyebrow">Local workspace</p>
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
