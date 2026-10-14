import { Documents } from "./Documents";
import { Collections } from "./Collections";
import { AddDocuments } from "./AddDocuments";
import { PageHeading, RouteLink } from "../components/Operations";
import { useAddDocumentDraft } from "./documents/draft";
import { Jobs } from "./Jobs";
import { Evaluations } from "./Evaluations";
import { Activity } from "./Activity";
import { Settings } from "./Settings";
export function OperationsRouter({ path }: { path: string }) {
  const parts = path
    .replace(/^\/dashboard\/?/, "")
    .split("/")
    .map(decodeURIComponent);
  const { draft } = useAddDocumentDraft();
  switch (parts[0]) {
    case "":
    case "collections":
      return <Collections id={parts[1]} />;
    case "documents":
      return parts[1] === "add" ? (
        <AddDocuments />
      ) : (
        <Documents sourceId={parts[1]} revisionId={parts[3]} />
      );
    case "models":
      return (
        <>
          <PageHeading title="Models">
            <p>Provider and model configuration for the entire workspace.</p>
          </PageHeading>
          <RouteLink to="/dashboard/settings">
            Open configuration settings
          </RouteLink>
          {draft.setupReturn && (
            <p>
              <RouteLink to={draft.setupReturn.route}>
                Return to Add documents
              </RouteLink>
            </p>
          )}
        </>
      );
    case "explore":
      return (
        <>
          <PageHeading title="Explore" />
          <p>
            Select a document’s Relationships view or follow evidence from
            Search to explore authorized relationships.
          </p>
          <RouteLink to="/dashboard/documents">Open Documents</RouteLink>
        </>
      );
    case "sources":
      return <Documents sourceId={parts[1]} revisionId={parts[3]} />;
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
