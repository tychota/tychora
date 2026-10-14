import type { Metric } from "../../api/operations";
export function Metrics({ metrics }: { metrics: Metric[] }) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Metric</th>
            <th>Value</th>
            <th>Samples</th>
            <th>Denominator</th>
          </tr>
        </thead>
        <tbody>
          {metrics.map((m) => (
            <tr key={m.name}>
              <td>{m.name}</td>
              <td>
                {m.value ??
                  `N/A · ${m.not_applicable_reason ?? "Unknown reason"}`}
              </td>
              <td>{m.sample_count}</td>
              <td>{m.denominator ?? "Unknown"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
