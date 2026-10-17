import { useEffect, useState, type ReactNode } from "react";
export function useRoute() {
  const [path, setPath] = useState(window.location.pathname);
  useEffect(() => {
    const update = () => setPath(window.location.pathname);
    window.addEventListener("popstate", update);
    return () => window.removeEventListener("popstate", update);
  }, []);
  const navigate = (next: string) => {
    history.pushState({}, "", next);
    window.dispatchEvent(new PopStateEvent("popstate"));
  };
  return { path, navigate };
}
export function RouteLink({
  to,
  children,
  ...props
}: { to: string; children: ReactNode } & Omit<
  React.AnchorHTMLAttributes<HTMLAnchorElement>,
  "href"
>) {
  const { navigate } = useRoute();
  return (
    <a
      {...props}
      href={to}
      onClick={(e) => {
        props.onClick?.(e);
        if (
          e.defaultPrevented ||
          e.button !== 0 ||
          e.metaKey ||
          e.ctrlKey ||
          e.shiftKey ||
          e.altKey
        )
          return;
        e.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}
