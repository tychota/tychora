import { Sources } from "./Sources";
import { Jobs } from "./Jobs";
import { Evaluations } from "./Evaluations";
import { Activity } from "./Activity";
import { Settings } from "./Settings";
export function OperationsRouter({ path }: { path: string }) {
  const parts = path
    .replace(/^\/dashboard\/?/, "")
    .split("/")
    .map(decodeURIComponent);
  switch (parts[0]) {
    case "sources":
      return <Sources sourceId={parts[1]} revisionId={parts[3]} />;
    case "jobs":
      return <Jobs id={parts[1]} />;
    case "evaluations":
      return <Evaluations id={parts[1]} />;
    case "activity":
      return <Activity />;
    case "runs":
      return <Activity id={parts[1]} />;
    case "settings":
      return <Settings />;
    default:
      return <p>Page unavailable.</p>;
  }
}
