import type {
  Handshake,
  ModelCatalog,
  ModelReadiness,
} from "../../api/contracts";
export interface Prerequisite {
  message: string;
  route: string;
  blocking: boolean;
}
/** Installed packages, assets, credentials and collection consent stay distinct. */
export function ingestionPrerequisites(
  catalog: ModelCatalog | null,
  readiness: ModelReadiness | null,
  handshake: Handshake | null,
  destination: string,
  selection: string[],
): Prerequisite[] {
  if (!catalog) return [];
  const result: Prerequisite[] = [];
  for (const s of selection) {
    const capability = catalog.selection_capabilities.find(
      (c) => c.selection === s,
    );
    if (capability && !capability.packages_available)
      result.push({
        message: `Required packages for ${s} are unavailable.`,
        route: "/dashboard/models",
        blocking: true,
      });
    const checked = readiness?.selections.find((c) => c.selection === s);
    if (checked && (checked.state !== "installed" || !checked.complete))
      result.push({
        message: `${s} assets: ${checked.state}${checked.complete ? "" : " · verification incomplete"}.`,
        route: "/dashboard/models",
        blocking: true,
      });
  }
  const text = catalog.providers.find((p) => p.slot === "text");
  if (text && !["disabled", "none"].includes(text.configured_provider)) {
    if (!text.package_available)
      result.push({
        message: "Configured text provider package is unavailable.",
        route: "/dashboard/models",
        blocking: true,
      });
    if (text.model_qualification === "unsupported")
      result.push({
        message: "Configured text model is unsupported.",
        route: "/dashboard/models",
        blocking: true,
      });
    if (catalog.offline)
      result.push({
        message:
          "The configured remote text provider is blocked by offline mode.",
        route: "/dashboard/models",
        blocking: true,
      });
    if (
      destination &&
      (!text.upload_allowed_collection_ids.includes(destination) ||
        !handshake?.scope.upload_providers.includes(text.configured_provider))
    )
      result.push({
        message:
          "Provider upload consent and caller permission are required for this destination.",
        route: `/dashboard/collections/${encodeURIComponent(destination)}`,
        blocking: true,
      });
    result.push({
      message:
        "Provider credentials have not been checked. Submission is validated by the server.",
      route: "/dashboard/models",
      blocking: false,
    });
  }
  return result;
}
export function requiredAssetSelections(
  catalog: ModelCatalog | null,
  format: string,
) {
  if (!catalog) return [];
  const text = catalog.providers.find((p) => p.slot === "text");
  return [
    ...new Set([
      ...(format === "pdf" || format === "docx" ? ["documents"] : []),
      ...(text &&
      !["disabled", "none"].includes(text.configured_provider) &&
      text.configured_model &&
      catalog.selections.includes(text.configured_model)
        ? [text.configured_model]
        : []),
    ]),
  ];
}
