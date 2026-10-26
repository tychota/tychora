"""Declared code identities traverse public ingestion and real evaluation paths."""

import json
from hashlib import sha256

from tests.code.conftest import git, make_repo
from tests.evaluation.conftest import (
    SyntheticCounter,
    add_repository,
    code_profile_config,
)
from tychora import Tychora
from tychora.adapters.sqlite.jobs import JobRepository
from tychora.application import query as query_module
from tychora.contracts.identity import canonical_bytes
from tychora.contracts.operations import ConfigurationVariant, EvaluationRequest
from tychora.evaluation.provenance import verify_result_locators
from tychora.evaluation.runner import EvaluationService
from tychora.ports.execution import ExecutionContext


def test_declared_code_definitions_and_graph_ablation(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    repeated = b"def entry(): return helper()\n\ndef helper(): return 7\n"
    (repo / "module.py").write_bytes(repeated)
    (repo / "twin.py").write_bytes(repeated)
    (repo / "overloads.ts").write_bytes(
        b"export function lookup(x: string): string;\n"
        b"export function lookup(x: number): number;\n"
        b"export function lookup(x: string | number) { return x; }\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Declared synthetic definitions")
    config = code_profile_config(tmp_path, "max_tokens=30000")
    with Tychora.open(
        tmp_path / "workspace", config=config, token_counter=SyntheticCounter()
    ) as kb:
        handle = add_repository(kb, repo, "code", "Synthetic Repository")
        status = kb.jobs.wait(handle.id, timeout=20)
        assert status.state == "succeeded", (status.error_code, status.message)
        owner = kb._workspace.owner

        def labels():
            return owner.db.execute(
                "SELECT s.source_id,r.revision_id,u.unit_id,sr.data_json "
                "FROM structured_records sr JOIN evidence_units u USING(unit_id) "
                "JOIN extractions e USING(extraction_id) "
                "JOIN revisions r USING(revision_id) JOIN sources s USING(source_id) "
                "WHERE sr.record_type='code_symbol' ORDER BY u.unit_id"
            ).fetchall()

        rows = kb._workspace.bridge.call(owner.run_write(labels, ExecutionContext()))
        assert len(rows) == 7
        source_id, revision_id = rows[0][:2]
        by_path = {}
        for _, _, uid, data in rows:
            record = json.loads(data)
            by_path.setdefault(record["relative_path"], []).append((uid, record))
        module_entry = next(
            item for item in by_path["module.py"] if item[1]["name"] == "entry"
        )
        twin_entry = next(
            item for item in by_path["twin.py"] if item[1]["name"] == "entry"
        )
        assert module_entry[1]["definition_key"] != twin_entry[1]["definition_key"]
        overloads = by_path["overloads.ts"]
        assert len({item[1]["definition_key"] for item in overloads}) == 3
        string_overload = next(
            item for item in overloads if item[1]["declaration_span"]["line_start"] == 1
        )

        def case(case_id, item, *, forbidden=(), exploratory=False):
            uid, record = item
            path = record["relative_path"]
            return {
                "case_id": case_id,
                "family_key": case_id,
                "split": "test",
                "benchmark": "integrity",
                "label_provenance": "declared synthetic definitions and canonical retained SQL IDs",
                "query": {
                    "question": "entry" if path.endswith(".py") else "lookup",
                    "exact_identifiers": []
                    if exploratory
                    else ["code.definition_key:" + record["definition_key"]],
                    "modalities": ["lexical"],
                    "mode": "lookup",
                    "limit": 8 if exploratory else 1,
                    "budgets": {"max_tokens": 30000, "tokenizer_id": "cl100k_base"},
                },
                "expected_source_ids": [source_id],
                "expected_revision_ids": [revision_id],
                "expected_unit_ids": [uid],
                "expected_definition_ids": [record["definition_key"]],
                "expected_file_ids": [f"{source_id}:{revision_id}:{path}"],
                "forbidden_definition_ids": [
                    other[1]["definition_key"] for other in forbidden
                ],
                "forbidden_file_ids": [
                    f"{source_id}:{revision_id}:{other[1]['relative_path']}"
                    for other in forbidden
                    if other[1]["relative_path"] != path
                ],
            }

        cases = (
            case("repeated-symbol-module", module_entry, forbidden=(twin_entry,)),
            case("repeated-symbol-twin", twin_entry, forbidden=(module_entry,)),
            case(
                "overload-string",
                string_overload,
                forbidden=tuple(item for item in overloads if item != string_overload),
            ),
            case("exploratory-entry", module_entry, exploratory=True),
        )
        cases_bytes = b"".join(canonical_bytes(item) + b"\n" for item in cases)
        gold = tmp_path / "code-gold"
        gold.mkdir()
        (gold / "cases.jsonl").write_bytes(cases_bytes)
        (gold / "manifest.json").write_bytes(
            canonical_bytes(
                {
                    "format": "tychora-gold@1",
                    "dataset_id": "synthetic-code-v1",
                    "name": "Synthetic code identity mechanics",
                    "version_sha": sha256(cases_bytes).hexdigest(),
                    "case_count": len(cases),
                }
            )
        )
        kb.evaluations.register_dataset(gold)
        for item in cases:
            result = kb.search(item["query"])
            if item["case_id"] != "exploratory-entry":
                expected_ref = {
                    "repeated-symbol-module": "module.py",
                    "repeated-symbol-twin": "twin.py",
                    "overload-string": "overloads.ts",
                }[item["case_id"]]
                assert result.segments[0].citations[0].source_ref == expected_ref
            valid = kb._workspace.bridge.call(
                owner.run_write(
                    lambda result=result: verify_result_locators(owner, result),
                    ExecutionContext(),
                )
            )
            assert valid.valid == valid.total
            if item["case_id"] == "repeated-symbol-module":
                segment = result.segments[0]
                forged = segment.citations[0].model_copy(
                    update={"source_ref": "twin.py"}
                )
                forged_result = result.model_copy(
                    update={
                        "segments": (
                            segment.model_copy(update={"citations": (forged,)}),
                        )
                    }
                )
                rejected = kb._workspace.bridge.call(
                    owner.run_write(
                        lambda forged_result=forged_result: verify_result_locators(
                            owner, forged_result
                        ),
                        ExecutionContext(),
                    )
                )
                assert rejected.valid < rejected.total
        snapshot = kb._workspace.bridge.call(
            owner.run_write(
                lambda: owner.store.resolve_snapshot(None), ExecutionContext()
            )
        )
        assert snapshot.family("graph") is not None
        observed = []
        original_augment = query_module.augment

        def observe(*args, **kwargs):
            result = original_augment(*args, **kwargs)
            observed.append(result)
            return result

        monkeypatch.setattr(query_module, "augment", observe)
        jobs = kb._workspace.bridge.call(
            owner.run_write(
                lambda: JobRepository(
                    owner.db,
                    artifacts=owner.artifacts,
                    workspace_id=owner.workspace_id,
                ),
                ExecutionContext(),
            )
        )
        evaluation = EvaluationService(owner, kb._workspace.services.query, jobs)
        completed = kb._workspace.bridge.call(
            evaluation.run(
                EvaluationRequest(
                    dataset_id="synthetic-code-v1",
                    split="test",
                    snapshot_id=snapshot.snapshot_id,
                    variants=(
                        ConfigurationVariant.from_mapping(
                            {
                                "name": "lexical",
                                "profile": "test",
                                "overrides": {"channels": ["lexical"]},
                            }
                        ),
                        ConfigurationVariant.from_mapping(
                            {
                                "name": "structural",
                                "profile": "test",
                                "overrides": {
                                    "channels": ["lexical", "structural_graph"]
                                },
                            }
                        ),
                    ),
                ),
                kb.scope,
                ExecutionContext(),
            )
        )
        assert completed.state == "succeeded"
        report = kb.evaluations.get(completed.id)
        assert set(report["cases"]) == {item["case_id"] for item in cases}
        for entry in report["cases"].values():
            assert [run["variant"] for run in entry["runs"]] == [
                "lexical",
                "structural",
            ]
            for run in entry["runs"]:
                metrics = {
                    metric["name"]: metric["value"]
                    for metric in run["metrics"]["metrics"]
                }
                assert metrics["definition_recall"] == 1.0
                assert metrics["file_recall"] == 1.0
                assert metrics["forbidden_leakage"] == 0.0
                assert metrics["locator_validity"] == 1.0
        assert len(observed) == len(cases)
        assert any(
            candidate.paths for page in observed for candidate in page.candidates
        )
