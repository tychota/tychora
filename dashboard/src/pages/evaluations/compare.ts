import type { Evaluation } from "../../api/operations";
import type { Json } from "../../api/contracts";
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object")
    return `{${Object.entries(value)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`)
      .join(",")}}`;
  return JSON.stringify(value) ?? "unknown";
}
export function compareEvaluations(left: Evaluation, right: Evaluation) {
  const context = (run: Evaluation): Record<string, Json> => ({
    dataset_id: run.dataset_id,
    split: run.report.split,
    snapshot_id: run.snapshot_id,
    config_sha: run.config_sha,
    policy_epoch: run.policy_epoch,
    lexical_statistics_epoch: run.lexical_statistics_epoch,
    hardware: run.hardware,
  });
  const a = context(left);
  const b = context(right);
  const differences = Object.keys(a)
    .filter((field) => canonical(a[field]) !== canonical(b[field]))
    .map((field) => ({ field, left: a[field], right: b[field] }));
  const rightMetrics = new Map(
    right.report.aggregate.metrics.map((m) => [m.name, m]),
  );
  const deltas = left.report.aggregate.metrics.flatMap((metric) => {
    const other = rightMetrics.get(metric.name);
    return typeof metric.value === "number" && typeof other?.value === "number"
      ? [
          {
            name: metric.name,
            left: metric.value,
            right: other.value,
            delta: other.value - metric.value,
          },
        ]
      : [];
  });
  return { differences, deltas };
}
