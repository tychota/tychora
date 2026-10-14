import { PageHeading, RouteLink } from "../components/Operations";
import { SourceForm } from "./sources/SourceForm";
export function AddDocuments() {
  return (
    <>
      <PageHeading title="Add documents">
        <p>
          Index text into one named collection. Your draft stays in memory
          during setup.
        </p>
        <RouteLink to="/dashboard/documents">Back to documents</RouteLink>
      </PageHeading>
      <SourceForm />
    </>
  );
}
