import type { Definition, Scope, Settings } from "../../api/contracts";
export function reviewAdmission(
  mode: Definition | undefined,
  scope: Scope | undefined,
): "included" | "excluded" | "unknown" {
  if (!mode || !scope) return "unknown";
  const d = mode.definition;
  if (
    !Array.isArray(d.allowed_uses) ||
    !d.allowed_uses.some((u) => typeof u === "string" && scope.uses.includes(u))
  )
    return "excluded";
  if (
    d.allow_derived_proxies === false ||
    d.derived_proxy_policy === "disabled" ||
    d.derived_proxy_policy === "accepted_only"
  )
    return "excluded";
  if (!Array.isArray(d.derived_proxy_review_states)) return "unknown";
  return d.derived_proxy_review_states.includes("needs_review")
    ? "included"
    : "excluded";
}
export function effectiveMode(settings: Settings | undefined) {
  const value = settings?.values.mode || settings?.values.default_mode;
  return typeof value === "string" ? value : "";
}
