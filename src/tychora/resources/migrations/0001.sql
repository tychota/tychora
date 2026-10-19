PRAGMA foreign_keys = ON;

CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE workspaces (
  workspace_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL CHECK(schema_version=2),
  active_snapshot_id TEXT REFERENCES snapshots(snapshot_id),
  policy_epoch INTEGER NOT NULL DEFAULT 0,
  lexical_statistics_epoch INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE collections (
  collection_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  slug TEXT NOT NULL,
  title TEXT NOT NULL,
  access_floor INTEGER NOT NULL DEFAULT 0 CHECK(access_floor BETWEEN 0 AND 3),
  policy_json TEXT NOT NULL CHECK(json_valid(policy_json)),
  UNIQUE(workspace_id, slug)
) STRICT;

CREATE TABLE kinds (
  kind_id TEXT PRIMARY KEY,
  definition_hash TEXT NOT NULL,
  label TEXT NOT NULL,
  description TEXT NOT NULL,
  role TEXT NOT NULL,
  definition_json TEXT NOT NULL CHECK(json_valid(definition_json))
) STRICT;

CREATE TABLE sources (
  source_id TEXT PRIMARY KEY,
  collection_id TEXT NOT NULL REFERENCES collections(collection_id),
  source_key TEXT NOT NULL,
  source_type TEXT NOT NULL CHECK(source_type IN ('document','repository')),
  registration_json TEXT NOT NULL CHECK(json_valid(registration_json)),
  access_floor INTEGER NOT NULL CHECK(access_floor BETWEEN 0 AND 3),
  withdrawn_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(collection_id, source_key)
) STRICT;

CREATE TABLE artifacts (
  artifact_sha TEXT PRIMARY KEY CHECK(length(artifact_sha)=64),
  size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
  media_type TEXT NOT NULL,
  storage_key TEXT NOT NULL UNIQUE,
  payload_state TEXT NOT NULL DEFAULT 'present'
    CHECK(payload_state IN ('present','delete_pending','deleted')),
  payload_deleted_at TEXT,
  created_at TEXT NOT NULL,
  CHECK((payload_state='deleted')=(payload_deleted_at IS NOT NULL))
) STRICT;

CREATE TABLE revisions (
  revision_id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(source_id),
  kind_id TEXT NOT NULL REFERENCES kinds(kind_id),
  original_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  edition_key TEXT NOT NULL,
  metadata_sha TEXT NOT NULL,
  title TEXT NOT NULL,
  published_on TEXT,
  effective_from TEXT,
  effective_until TEXT,
  access_rank INTEGER NOT NULL CHECK(access_rank BETWEEN 0 AND 3),
  declared_review_state TEXT NOT NULL,
  allowed_uses_json TEXT NOT NULL CHECK(json_valid(allowed_uses_json)),
  metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
  captured_at TEXT NOT NULL,
  CHECK(effective_from IS NULL OR effective_until IS NULL OR effective_from<effective_until),
  UNIQUE(source_id, original_sha, edition_key, metadata_sha),
  UNIQUE(revision_id, source_id)
) STRICT;

CREATE TABLE revision_artifacts (
  revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
  role TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  artifact_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  PRIMARY KEY(revision_id, role, source_ref)
) STRICT;

CREATE TABLE extractions (
  extraction_id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
  parser_fingerprint TEXT NOT NULL,
  normalizer_fingerprint TEXT NOT NULL,
  document_artifact_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  assertions_artifact_sha TEXT REFERENCES artifacts(artifact_sha),
  diagnostics_artifact_sha TEXT REFERENCES artifacts(artifact_sha),
  created_at TEXT NOT NULL,
  UNIQUE(revision_id, parser_fingerprint, normalizer_fingerprint),
  UNIQUE(extraction_id, revision_id)
) STRICT;

CREATE TABLE evidence_units (
  unit_id TEXT PRIMARY KEY,
  extraction_id TEXT NOT NULL REFERENCES extractions(extraction_id),
  parent_id TEXT,
  ordinal INTEGER NOT NULL CHECK(ordinal>=0),
  unit_type TEXT NOT NULL,
  native_ref TEXT NOT NULL,
  boundary_key TEXT NOT NULL,
  heading_path_json TEXT NOT NULL CHECK(json_valid(heading_path_json)),
  text TEXT NOT NULL,
  text_sha TEXT NOT NULL,
  text_origin TEXT NOT NULL CHECK(text_origin IN ('source','ocr','derived')),
  citable INTEGER NOT NULL CHECK(citable IN (0,1)),
  declared_review_state TEXT NOT NULL,
  attributes_json TEXT NOT NULL CHECK(json_valid(attributes_json)),
  UNIQUE(extraction_id, ordinal),
  UNIQUE(unit_id, extraction_id),
  FOREIGN KEY(parent_id, extraction_id)
    REFERENCES evidence_units(unit_id, extraction_id) DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE locators (
  locator_id TEXT PRIMARY KEY,
  unit_id TEXT NOT NULL REFERENCES evidence_units(unit_id),
  fragment_ordinal INTEGER NOT NULL,
  locator_type TEXT NOT NULL CHECK(locator_type IN ('document','code','text','image')),
  artifact_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  source_ref TEXT NOT NULL,
  physical_page INTEGER CHECK(physical_page IS NULL OR physical_page>=1),
  page_label TEXT,
  line_start INTEGER,
  line_end INTEGER,
  byte_start INTEGER,
  byte_end INTEGER,
  geometry_json TEXT CHECK(geometry_json IS NULL OR json_valid(geometry_json)),
  details_json TEXT NOT NULL CHECK(json_valid(details_json)),
  UNIQUE(unit_id, fragment_ordinal)
) STRICT;

CREATE TABLE assets (
  asset_id TEXT PRIMARY KEY,
  extraction_id TEXT NOT NULL REFERENCES extractions(extraction_id),
  artifact_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  asset_type TEXT NOT NULL,
  native_ref TEXT NOT NULL,
  locator_json TEXT NOT NULL CHECK(json_valid(locator_json)),
  caption TEXT NOT NULL DEFAULT '',
  declared_review_state TEXT NOT NULL,
  UNIQUE(extraction_id, native_ref, artifact_sha)
) STRICT;

CREATE TABLE unit_assets (
  unit_id TEXT NOT NULL REFERENCES evidence_units(unit_id),
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  role TEXT NOT NULL,
  PRIMARY KEY(unit_id, asset_id, role)
) STRICT;

CREATE TABLE asset_proxies (
  proxy_unit_id TEXT PRIMARY KEY REFERENCES evidence_units(unit_id),
  asset_id TEXT NOT NULL REFERENCES assets(asset_id),
  origin_type TEXT NOT NULL CHECK(origin_type IN ('caption_copy','ocr','description','structured_values')),
  derivation_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha)
) STRICT;

CREATE TABLE asset_proxy_dependencies (
  proxy_unit_id TEXT NOT NULL REFERENCES asset_proxies(proxy_unit_id),
  required_unit_id TEXT NOT NULL REFERENCES evidence_units(unit_id),
  PRIMARY KEY(proxy_unit_id,required_unit_id),
  CHECK(proxy_unit_id<>required_unit_id)
) STRICT;

CREATE TABLE structured_records (
  unit_id TEXT PRIMARY KEY REFERENCES evidence_units(unit_id),
  record_type TEXT NOT NULL,
  schema_hash TEXT NOT NULL,
  data_json TEXT NOT NULL CHECK(json_valid(data_json))
) STRICT;

CREATE TABLE chunks (
  chunk_id TEXT PRIMARY KEY,
  unit_id TEXT NOT NULL,
  extraction_id TEXT NOT NULL,
  ordinal_in_unit INTEGER NOT NULL,
  char_start INTEGER NOT NULL CHECK(char_start>=0),
  char_end INTEGER NOT NULL CHECK(char_end>char_start),
  context_group_key TEXT NOT NULL,
  chunker_fingerprint TEXT NOT NULL,
  token_count INTEGER NOT NULL CHECK(token_count>0),
  UNIQUE(unit_id, ordinal_in_unit, chunker_fingerprint),
  FOREIGN KEY(unit_id, extraction_id) REFERENCES evidence_units(unit_id, extraction_id)
) STRICT;

CREATE TABLE identifiers (
  namespace TEXT NOT NULL,
  normalized_value TEXT NOT NULL,
  original_value TEXT NOT NULL,
  unit_id TEXT NOT NULL REFERENCES evidence_units(unit_id),
  PRIMARY KEY(namespace, normalized_value, unit_id)
) STRICT;

CREATE TABLE facet_definitions (
  facet_key TEXT PRIMARY KEY,
  value_type TEXT NOT NULL CHECK(value_type IN ('text','number','boolean','date')),
  operators_json TEXT NOT NULL CHECK(json_valid(operators_json)),
  filter_scope TEXT NOT NULL DEFAULT 'all_units'
    CHECK(filter_scope IN ('all_units','matched_candidates')),
  definition_hash TEXT NOT NULL
) STRICT;

CREATE TABLE facet_values (
  id INTEGER PRIMARY KEY,
  revision_id TEXT REFERENCES revisions(revision_id),
  unit_id TEXT REFERENCES evidence_units(unit_id),
  facet_key TEXT NOT NULL REFERENCES facet_definitions(facet_key),
  ordinal INTEGER NOT NULL DEFAULT 0,
  text_value TEXT,
  number_value REAL,
  boolean_value INTEGER CHECK(boolean_value IS NULL OR boolean_value IN (0,1)),
  CHECK((revision_id IS NOT NULL)+(unit_id IS NOT NULL)=1),
  CHECK((text_value IS NOT NULL)+(number_value IS NOT NULL)+(boolean_value IS NOT NULL)=1)
) STRICT;
CREATE UNIQUE INDEX facets_revision_unique ON facet_values(revision_id,facet_key,ordinal)
  WHERE revision_id IS NOT NULL;
CREATE UNIQUE INDEX facets_unit_unique ON facet_values(unit_id,facet_key,ordinal)
  WHERE unit_id IS NOT NULL;
CREATE INDEX facets_text ON facet_values(facet_key,text_value);
CREATE INDEX facets_number ON facet_values(facet_key,number_value);

CREATE TABLE review_events (
  review_event_id INTEGER PRIMARY KEY,
  revision_id TEXT REFERENCES revisions(revision_id),
  unit_id TEXT REFERENCES evidence_units(unit_id),
  asset_id TEXT REFERENCES assets(asset_id),
  state TEXT NOT NULL CHECK(state IN ('unreviewed','needs_review','accepted','rejected')),
  actor_id TEXT NOT NULL,
  reason_artifact_sha TEXT REFERENCES artifacts(artifact_sha),
  created_at TEXT NOT NULL,
  CHECK((revision_id IS NOT NULL)+(unit_id IS NOT NULL)+(asset_id IS NOT NULL)=1)
) STRICT;
CREATE INDEX review_revision_latest ON review_events(revision_id,review_event_id DESC);
CREATE INDEX review_unit_latest ON review_events(unit_id,review_event_id DESC);
CREATE INDEX review_asset_latest ON review_events(asset_id,review_event_id DESC);

CREATE TABLE assertion_decisions (
  decision_id INTEGER PRIMARY KEY,
  assertion_id TEXT NOT NULL,
  provenance_artifact_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  provenance_record_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('recorded','proposed','confirmed','rejected')),
  actor_id TEXT NOT NULL,
  reason_artifact_sha TEXT REFERENCES artifacts(artifact_sha),
  created_at TEXT NOT NULL
) STRICT;
CREATE INDEX assertion_decisions_latest ON assertion_decisions(assertion_id,decision_id DESC);

CREATE TABLE index_families (
  family_id TEXT PRIMARY KEY,
  channel TEXT NOT NULL CHECK(channel IN ('lexical','text','image','graph')),
  backend TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  dimension INTEGER CHECK(dimension IS NULL OR dimension>0),
  config_sha TEXT NOT NULL,
  config_json TEXT NOT NULL CHECK(json_valid(config_json)),
  backend_namespace TEXT NOT NULL,
  identity_sha TEXT NOT NULL CHECK(length(identity_sha)=64),
  creation_nonce TEXT NOT NULL CHECK(length(creation_nonce)=36),
  created_at TEXT NOT NULL,
  UNIQUE(channel,identity_sha,creation_nonce)
) STRICT;
CREATE INDEX families_identity ON index_families(channel,identity_sha,created_at);

CREATE TABLE index_batches (
  batch_id TEXT PRIMARY KEY,
  family_id TEXT NOT NULL REFERENCES index_families(family_id),
  extraction_id TEXT REFERENCES extractions(extraction_id),
  input_manifest_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  expected_count INTEGER NOT NULL CHECK(expected_count>=0),
  actual_count INTEGER CHECK(actual_count IS NULL OR actual_count>=0),
  entry_ids_sha TEXT,
  state TEXT NOT NULL CHECK(state IN ('pending','building','ready','failed','removed')),
  receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
  vectors_sha TEXT REFERENCES artifacts(artifact_sha),
  UNIQUE(batch_id,family_id),
  UNIQUE(family_id,extraction_id)
) STRICT;

CREATE TABLE vector_records (
  record_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES index_batches(batch_id),
  chunk_id TEXT REFERENCES chunks(chunk_id),
  asset_id TEXT REFERENCES assets(asset_id),
  input_sha TEXT NOT NULL,
  row_index INTEGER NOT NULL CHECK(row_index>=0),
  CHECK((chunk_id IS NOT NULL)+(asset_id IS NOT NULL)=1),
  UNIQUE(batch_id,chunk_id),
  UNIQUE(batch_id,asset_id),
  UNIQUE(batch_id,row_index)
) STRICT;

CREATE TABLE lexical_records (
  rowid INTEGER PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES index_batches(batch_id),
  chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
  identifiers TEXT NOT NULL,
  title TEXT NOT NULL,
  headings TEXT NOT NULL,
  body TEXT NOT NULL,
  context TEXT NOT NULL,
  UNIQUE(batch_id,chunk_id)
) STRICT;
CREATE VIRTUAL TABLE lexical_fts USING fts5(
  identifiers,title,headings,body,context,
  content='lexical_records',content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER lexical_insert AFTER INSERT ON lexical_records BEGIN
  INSERT INTO lexical_fts(rowid,identifiers,title,headings,body,context)
  VALUES(new.rowid,new.identifiers,new.title,new.headings,new.body,new.context);
END;
CREATE TRIGGER lexical_delete AFTER DELETE ON lexical_records BEGIN
  INSERT INTO lexical_fts(lexical_fts,rowid,identifiers,title,headings,body,context)
  VALUES('delete',old.rowid,old.identifiers,old.title,old.headings,old.body,old.context);
END;
CREATE TRIGGER lexical_no_update BEFORE UPDATE ON lexical_records BEGIN
  SELECT RAISE(ABORT,'lexical records are immutable; replace the batch');
END;

CREATE TABLE snapshots (
  snapshot_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  parent_snapshot_id TEXT REFERENCES snapshots(snapshot_id),
  membership_sha TEXT NOT NULL,
  config_sha TEXT NOT NULL,
  manifest_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  published_at TEXT NOT NULL,
  job_id TEXT,
  member_count INTEGER CHECK(member_count IS NULL OR member_count>=0)
) STRICT;

CREATE TABLE snapshot_members (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  source_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  extraction_id TEXT NOT NULL,
  is_default INTEGER NOT NULL CHECK(is_default IN (0,1)),
  PRIMARY KEY(snapshot_id,revision_id),
  FOREIGN KEY(revision_id,source_id) REFERENCES revisions(revision_id,source_id),
  FOREIGN KEY(extraction_id,revision_id) REFERENCES extractions(extraction_id,revision_id)
) STRICT;
CREATE UNIQUE INDEX snapshot_default_source ON snapshot_members(snapshot_id,source_id)
  WHERE is_default=1;

CREATE TABLE snapshot_indexes (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  slot TEXT NOT NULL,
  family_id TEXT NOT NULL REFERENCES index_families(family_id),
  required INTEGER NOT NULL CHECK(required IN (0,1)),
  PRIMARY KEY(snapshot_id,slot),
  UNIQUE(snapshot_id,slot,family_id)
) STRICT;

CREATE TABLE snapshot_batches (
  snapshot_id TEXT NOT NULL,
  slot TEXT NOT NULL,
  family_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  PRIMARY KEY(snapshot_id,slot,batch_id),
  FOREIGN KEY(snapshot_id,slot,family_id) REFERENCES snapshot_indexes(snapshot_id,slot,family_id),
  FOREIGN KEY(batch_id,family_id) REFERENCES index_batches(batch_id,family_id)
) STRICT;

CREATE TABLE membership_pages (
  page_id TEXT PRIMARY KEY,
  bucket INTEGER NOT NULL CHECK(bucket BETWEEN 0 AND 255),
  manifest_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  member_count INTEGER NOT NULL CHECK(member_count>=0),
  UNIQUE(page_id,bucket)
) STRICT;
CREATE TABLE page_members (
  page_id TEXT NOT NULL REFERENCES membership_pages(page_id),
  source_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  extraction_id TEXT NOT NULL,
  is_default INTEGER NOT NULL CHECK(is_default IN (0,1)),
  PRIMARY KEY(page_id,revision_id),
  FOREIGN KEY(revision_id,source_id) REFERENCES revisions(revision_id,source_id),
  FOREIGN KEY(extraction_id,revision_id) REFERENCES extractions(extraction_id,revision_id)
) STRICT;
CREATE INDEX page_members_extraction ON page_members(extraction_id,page_id);
CREATE TABLE page_batches (
  page_id TEXT NOT NULL REFERENCES membership_pages(page_id),
  slot TEXT NOT NULL,
  batch_id TEXT NOT NULL REFERENCES index_batches(batch_id),
  PRIMARY KEY(page_id,slot,batch_id)
) STRICT;
CREATE INDEX page_batches_batch ON page_batches(batch_id);
CREATE TABLE snapshot_pages (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  bucket INTEGER NOT NULL CHECK(bucket BETWEEN 0 AND 255),
  page_id TEXT NOT NULL,
  PRIMARY KEY(snapshot_id,bucket),
  FOREIGN KEY(page_id,bucket) REFERENCES membership_pages(page_id,bucket)
) STRICT;
CREATE INDEX snapshot_pages_page ON snapshot_pages(page_id,bucket);
CREATE TABLE snapshot_root_batches (
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  slot TEXT NOT NULL,
  batch_id TEXT NOT NULL REFERENCES index_batches(batch_id),
  PRIMARY KEY(snapshot_id,slot,batch_id)
) STRICT;
CREATE INDEX snapshot_root_batches_batch ON snapshot_root_batches(batch_id);

CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  principal_id TEXT NOT NULL,
  idempotency_key TEXT,
  request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
  effective_config_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  captured_inputs_sha TEXT REFERENCES artifacts(artifact_sha),
  state TEXT NOT NULL CHECK(state IN ('queued','running','cancelling','cancelled','failed','complete')),
  base_snapshot_id TEXT REFERENCES snapshots(snapshot_id),
  result_snapshot_id TEXT REFERENCES snapshots(snapshot_id),
  request_artifact_sha TEXT REFERENCES artifacts(artifact_sha),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  error_code TEXT,
  UNIQUE(principal_id,operation,idempotency_key)
) STRICT;

CREATE TABLE original_payload_tombstones (
  revision_id TEXT NOT NULL REFERENCES revisions(revision_id),
  artifact_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  removal_job_id TEXT NOT NULL REFERENCES jobs(job_id),
  removed_at TEXT NOT NULL,
  PRIMARY KEY(revision_id, artifact_sha)
) STRICT;
CREATE INDEX tombstones_artifact ON original_payload_tombstones(artifact_sha);

CREATE TABLE job_steps (
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  step_key TEXT NOT NULL,
  input_sha TEXT NOT NULL,
  state TEXT NOT NULL,
  output_artifact_sha TEXT REFERENCES artifacts(artifact_sha),
  completed_count INTEGER NOT NULL DEFAULT 0,
  total_count INTEGER,
  attempt INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(job_id,step_key)
) STRICT;

CREATE TABLE job_events (
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  sequence INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
  created_at TEXT NOT NULL,
  PRIMARY KEY(job_id,sequence)
) STRICT;

CREATE TABLE query_runs (
  run_id TEXT PRIMARY KEY,
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  principal_id TEXT NOT NULL,
  scope_digest TEXT NOT NULL,
  policy_epoch INTEGER NOT NULL,
  lexical_statistics_epoch INTEGER NOT NULL,
  config_sha TEXT NOT NULL,
  state TEXT NOT NULL,
  metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)),
  result_ids_json TEXT NOT NULL CHECK(json_valid(result_ids_json)),
  retained_payload_sha TEXT REFERENCES artifacts(artifact_sha),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
) STRICT;

CREATE TABLE evaluation_datasets (
  dataset_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version_sha TEXT NOT NULL UNIQUE,
  manifest_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE evaluation_cases (
  dataset_id TEXT NOT NULL REFERENCES evaluation_datasets(dataset_id),
  case_id TEXT NOT NULL,
  split TEXT NOT NULL CHECK(split IN ('development','test')),
  family_key TEXT NOT NULL,
  payload_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  PRIMARY KEY(dataset_id,case_id)
) STRICT;

CREATE TABLE evaluation_runs (
  evaluation_id TEXT PRIMARY KEY,
  dataset_id TEXT NOT NULL REFERENCES evaluation_datasets(dataset_id),
  snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  policy_epoch INTEGER NOT NULL,
  lexical_statistics_epoch INTEGER NOT NULL,
  config_sha TEXT NOT NULL,
  hardware_json TEXT NOT NULL CHECK(json_valid(hardware_json)),
  aggregate_json TEXT NOT NULL CHECK(json_valid(aggregate_json)),
  created_at TEXT NOT NULL
) STRICT;

CREATE TABLE evaluation_results (
  evaluation_id TEXT NOT NULL REFERENCES evaluation_runs(evaluation_id),
  case_id TEXT NOT NULL,
  result_artifact_sha TEXT NOT NULL REFERENCES artifacts(artifact_sha),
  metrics_json TEXT NOT NULL CHECK(json_valid(metrics_json)),
  PRIMARY KEY(evaluation_id,case_id)
) STRICT;

CREATE INDEX revisions_source_dates ON revisions(source_id,published_on,effective_from,effective_until);
CREATE INDEX units_extraction_boundary ON evidence_units(extraction_id,boundary_key,ordinal);
CREATE INDEX chunks_extraction_group ON chunks(extraction_id,context_group_key);
CREATE INDEX batches_family_extraction ON index_batches(family_id,extraction_id,state);
CREATE INDEX vector_chunk ON vector_records(chunk_id,batch_id);
CREATE INDEX vector_asset ON vector_records(asset_id,batch_id);
CREATE INDEX members_extraction ON snapshot_members(extraction_id,snapshot_id);
CREATE INDEX queries_expiration ON query_runs(expires_at);
