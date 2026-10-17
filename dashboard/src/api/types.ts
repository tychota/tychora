/** Job types shared by the contract and operation modules, kept apart to avoid a cycle. */
export type JobState =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";
export interface JobHandle {
  id: string;
  operation: string;
  state: JobState;
}
