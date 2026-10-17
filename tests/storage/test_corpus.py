"""Real SQLite transactions with verified synthetic artifact bytes."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from hashlib import sha256
from typing import BinaryIO, cast

import pytest

from tests.helpers.corpus import artifact, reader, registry, seed_bundle
from tychora.adapters.sqlite.connection import open_corpus
from tychora.adapters.sqlite.corpus import CorpusStore
from tychora.contracts.artifacts import ArtifactRef, ArtifactStat
from tychora.contracts.base import FrozenJsonMap
from tychora.contracts.corpus import (
    AssetProxyRow,
    AssetRow,
    FacetRow,
    ReviewTarget,
    StructuredRecordRow,
    UnitAssetRow,
)
from tychora.contracts.errors import TychoraError
from tychora.contracts.evidence import CodeLocator, DocumentLocator, ImageLocator


def test_commit_hydrate_and_exact_retry(tmp_path):
    from tests.helpers.corpus import reader, registry, seed_bundle

    bundle = seed_bundle("Record l’été.\nKeep it exact.")
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    store = CorpusStore(db, registry=registry(), artifacts=reader())
    assert store.commit_extraction(bundle) == bundle.extraction.extraction_id
    assert store.commit_extraction(bundle) == bundle.extraction.extraction_id
    assert store.hydrate((bundle.units[0].unit_id,)) == bundle.units
    assert db.execute("SELECT COUNT(*) FROM evidence_units").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert not db.in_transaction


@pytest.fixture
def corpus(tmp_path):
    db = open_corpus(tmp_path / "corpus.sqlite3", initialize=True)
    yield CorpusStore(db, registry=registry(), artifacts=reader())
    db.close()


@pytest.mark.parametrize(
    "case",
    [
        "text_hash",
        "metadata_schema",
        "metadata_hash",
        "date",
        "interval",
        "wrong_extraction",
        "parent_cycle",
        "chunk_text",
        "chunk_bound",
        "locator_bound",
        "locator_binding",
        "locator_rows",
        "missing_bytes",
        "wrong_bytes",
        "wrong_size",
        "facet_type",
        "record_schema",
    ],
)
def test_invalid_bundle_rolls_back_every_row(corpus, case):
    b = seed_bundle()
    u = b.units[0]
    if case == "text_hash":
        b = b.model_copy(
            update={"units": (u.model_copy(update={"text_sha": "0" * 64}),)}
        )
    elif case == "metadata_schema":
        b = b.model_copy(
            update={
                "revision": b.revision.model_copy(
                    update={"metadata_json": FrozenJsonMap({"title": "Incomplete"})}
                )
            }
        )
    elif case == "metadata_hash":
        b = b.model_copy(
            update={
                "revision": b.revision.model_copy(update={"metadata_sha": "0" * 64})
            }
        )
    elif case in ("date", "interval"):
        changes = (
            {"published_on": "2024-02-30"}
            if case == "date"
            else {"effective_from": "2024-02-02", "effective_until": "2024-02-01"}
        )
        b = b.model_copy(update={"revision": b.revision.model_copy(update=changes)})
    elif case == "wrong_extraction":
        b = b.model_copy(
            update={"units": (u.model_copy(update={"extraction_id": "elsewhere"}),)}
        )
    elif case == "parent_cycle":
        b = b.model_copy(
            update={"units": (u.model_copy(update={"parent_id": u.unit_id}),)}
        )
    elif case in ("chunk_text", "chunk_bound"):
        changes = {"text": "invented"} if case == "chunk_text" else {"char_end": 999}
        b = b.model_copy(update={"chunks": (b.chunks[0].model_copy(update=changes),)})
    elif case in ("locator_bound", "locator_binding"):
        changes = (
            {"char_end": 999}
            if case == "locator_bound"
            else {"artifact_ref": b.extraction.document_artifact_sha}
        )
        loc = u.locators[0].model_copy(update=changes)
        b = b.model_copy(
            update={
                "units": (u.model_copy(update={"locators": (loc,)}),),
                "locators": (b.locators[0].model_copy(update={"locator": loc}),),
            }
        )
    elif case == "locator_rows":
        b = b.model_copy(update={"locators": ()})
    elif case in ("missing_bytes", "wrong_bytes", "wrong_size"):
        ref = b.artifacts[0]
        if case == "missing_bytes":
            ref = ref.model_copy(update={"storage_key": "absent-file"})
        elif case == "wrong_bytes":
            other = artifact(b"different bytes")
            ref = ref.model_copy(update={"storage_key": other.storage_key})
        else:
            ref = ref.model_copy(update={"size_bytes": ref.size_bytes + 1})
        b = b.model_copy(update={"artifacts": (ref, b.artifacts[1])})
    elif case == "facet_type":
        b = b.model_copy(
            update={
                "facets": (
                    FacetRow(unit_id=u.unit_id, facet_key="scope.project", value=5),
                )
            }
        )
    elif case == "record_schema":
        b = b.model_copy(
            update={
                "records": (
                    StructuredRecordRow(
                        unit_id=u.unit_id,
                        record_type="invented",
                        schema_hash="0" * 64,
                        data_json=FrozenJsonMap(),
                    ),
                )
            }
        )
    with pytest.raises(TychoraError):
        corpus.commit_extraction(b)
    assert corpus.db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0
    assert not corpus.db.in_transaction


def test_immutable_collision_leaves_original_unchanged(corpus):
    b = seed_bundle()
    corpus.commit_extraction(b)
    altered = b.model_copy(
        update={"units": (b.units[0].model_copy(update={"boundary_key": "changed"}),)}
    )
    with pytest.raises(TychoraError, match="Immutable"):
        corpus.commit_extraction(altered)
    assert corpus.hydrate((b.units[0].unit_id,)) == b.units


def test_exact_retry_cannot_append_new_rows(corpus):
    b = seed_bundle()
    corpus.commit_extraction(b)
    new = b.units[0].model_copy(
        update={"unit_id": "new-unit", "ordinal": 1, "locators": ()}
    )
    with pytest.raises(TychoraError):
        corpus.commit_extraction(b.model_copy(update={"units": (*b.units, new)}))
    assert corpus.db.execute("SELECT COUNT(*) FROM evidence_units").fetchone()[0] == 1


def proxy_bundle(kind="manual@1"):
    b = seed_bundle(kind=kind)
    image = artifact(b"synthetic image payload", "image/png")
    derivation = artifact(b'{"method":"literal caption copy"}', "application/json")
    loc = ImageLocator(artifact_ref=image.sha256, native_ref="figure:1")
    asset = AssetRow(
        asset_id="asset-1",
        extraction_id=b.extraction.extraction_id,
        artifact_sha=image.sha256,
        asset_type="figure",
        native_ref="figure:1",
        locator=loc,
    )
    proxy = b.units[0].model_copy(
        update={
            "unit_id": "proxy-1",
            "ordinal": 1,
            "citable": False,
            "text_origin": "derived",
            "locators": (),
        }
    )
    row = AssetProxyRow(
        proxy_unit_id="proxy-1",
        asset_id="asset-1",
        origin_type="caption_copy",
        derivation_sha=derivation.sha256,
        required_unit_ids=frozenset({b.units[0].unit_id}),
    )
    return b.model_copy(
        update={
            "artifacts": (*b.artifacts, image, derivation),
            "assets": (asset,),
            "units": (*b.units, proxy),
            "proxies": (row,),
            "unit_assets": (
                UnitAssetRow(
                    unit_id=b.units[0].unit_id, asset_id="asset-1", role="caption"
                ),
            ),
        }
    )


def test_proxy_and_typed_facets_round_trip(corpus):
    b = proxy_bundle()
    b = b.model_copy(
        update={
            "facets": (
                FacetRow(
                    revision_id=b.revision.revision_id,
                    facet_key="scope.project",
                    value="example",
                ),
            )
        }
    )
    corpus.commit_extraction(b)
    corpus.commit_extraction(b)
    assert corpus.db.execute("SELECT text_value FROM facet_values").fetchall() == [
        ("example",)
    ]
    assert corpus.db.execute(
        "SELECT required_unit_id FROM asset_proxy_dependencies"
    ).fetchall() == [(b.units[0].unit_id,)]
    assert corpus.hydrate(("proxy-1",))[0].citable is False


@pytest.mark.parametrize(
    "case",
    [
        "citable",
        "empty_dependencies",
        "self_dependency",
        "proxy_dependency",
        "missing_dependency",
        "derived_dependency",
        "cross_asset",
        "unlinked_asset",
        "shared_derivation",
        "outside_unit_asset",
    ],
)
def test_proxy_provenance_constraints(corpus, case):
    b = proxy_bundle()
    p = b.proxies[0]
    if case == "citable":
        b = b.model_copy(
            update={
                "units": (b.units[0], b.units[1].model_copy(update={"citable": True}))
            }
        )
    elif case in (
        "empty_dependencies",
        "self_dependency",
        "missing_dependency",
        "proxy_dependency",
    ):
        deps = {
            "empty_dependencies": (),
            "self_dependency": ("proxy-1",),
            "missing_dependency": ("missing",),
            "proxy_dependency": ("proxy-2",),
        }[case]
        b = b.model_copy(
            update={
                "proxies": (
                    p.model_copy(update={"required_unit_ids": frozenset(deps)}),
                )
            }
        )
        if case == "proxy_dependency":
            b = b.model_copy(
                update={
                    "units": (
                        *b.units,
                        b.units[1].model_copy(
                            update={"unit_id": "proxy-2", "ordinal": 2}
                        ),
                    ),
                    "proxies": (
                        *b.proxies,
                        p.model_copy(update={"proxy_unit_id": "proxy-2"}),
                    ),
                }
            )
    elif case == "derived_dependency":
        b = b.model_copy(
            update={
                "units": (
                    b.units[0].model_copy(update={"text_origin": "derived"}),
                    b.units[1],
                )
            }
        )
    elif case == "cross_asset":
        b = b.model_copy(
            update={
                "assets": (b.assets[0].model_copy(update={"extraction_id": "foreign"}),)
            }
        )
    elif case == "unlinked_asset":
        b = b.model_copy(
            update={"proxies": (p.model_copy(update={"asset_id": "foreign"}),)}
        )
    elif case == "shared_derivation":
        b = b.model_copy(
            update={
                "proxies": (
                    p.model_copy(update={"derivation_sha": b.revision.original_sha}),
                )
            }
        )
    else:
        b = b.model_copy(
            update={
                "unit_assets": (
                    b.unit_assets[0].model_copy(update={"unit_id": "foreign"}),
                )
            }
        )
    with pytest.raises(TychoraError):
        corpus.commit_extraction(b)
    assert corpus.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 0


def epoch(corpus):
    return corpus.db.execute("SELECT policy_epoch FROM workspaces").fetchone()[0]


def test_revision_rejection_dominates_later_child_acceptance(corpus):
    b = seed_bundle()
    corpus.commit_extraction(b)
    target = ReviewTarget(unit_id=b.units[0].unit_id)
    first = corpus.record_review(
        ReviewTarget(revision_id=b.revision.revision_id), "rejected", "reviewer"
    )
    second = corpus.record_review(target, "accepted", "reviewer")
    assert second > first
    assert epoch(corpus) == 2
    assert corpus.hydrate((b.units[0].unit_id,))[0].review_state == "rejected"
    corpus.record_review(
        ReviewTarget(revision_id=b.revision.revision_id), "accepted", "reviewer"
    )
    assert corpus.hydrate((b.units[0].unit_id,))[0].review_state == "accepted"
    assert (
        corpus.db.execute(
            "SELECT declared_review_state FROM evidence_units"
        ).fetchone()[0]
        == "unreviewed"
    )


@pytest.mark.parametrize("case", ["unknown", "bad_state", "empty_actor"])
def test_invalid_review_never_advances_epoch(corpus, case):
    b = seed_bundle()
    corpus.commit_extraction(b)
    target = ReviewTarget(
        unit_id="missing" if case == "unknown" else b.units[0].unit_id
    )
    with pytest.raises(TychoraError):
        corpus.record_review(
            target,
            "invented" if case == "bad_state" else "accepted",
            "" if case == "empty_actor" else "reviewer",
        )
    assert epoch(corpus) == 0
    assert corpus.db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0] == 0


def test_naive_decision_time_is_refused_with_a_reason(corpus):
    b = seed_bundle()
    corpus.commit_extraction(b)
    with pytest.raises(TychoraError) as error:
        corpus.record_review(
            ReviewTarget(unit_id=b.units[0].unit_id),
            "accepted",
            "reviewer",
            decided_at=datetime(2026, 1, 1),  # noqa: DTZ001
        )
    assert (error.value.code, error.value.reason) == (
        "INVALID_REQUEST",
        "review.time_naive",
    )
    assert epoch(corpus) == 0


def test_registration_access_floor_can_only_rise(corpus):
    b = seed_bundle()
    corpus.commit_extraction(b)
    elevated = b.model_copy(
        update={
            "source": b.source.model_copy(update={"access_floor": 2}),
            "collection": b.collection.model_copy(update={"access_floor": 1}),
        }
    )
    corpus.commit_extraction(elevated)
    assert epoch(corpus) == 1
    before = corpus.db.execute("SELECT * FROM revisions").fetchall()
    with pytest.raises(TychoraError) as stale:
        corpus.commit_extraction(b)
    assert stale.value.code == "SCOPE_CHANGED"
    assert corpus.db.execute("SELECT * FROM revisions").fetchall() == before
    assert epoch(corpus) == 1
    assert corpus.db.execute("SELECT access_floor FROM sources").fetchone()[0] == 2
    assert corpus.db.execute("SELECT access_floor FROM collections").fetchone()[0] == 1


def test_withdrawal_and_explicit_access_mutations_are_atomic(corpus):
    b = seed_bundle()
    corpus.commit_extraction(b)
    corpus.raise_access_floor(ReviewTarget(revision_id=b.revision.revision_id), 3)
    assert epoch(corpus) == 1
    corpus.withdraw_source(b.source.source_id)
    assert epoch(corpus) == 2
    assert (
        corpus.db.execute("SELECT withdrawn_at FROM sources").fetchone()[0] is not None
    )
    corpus.withdraw_source(b.source.source_id)
    assert epoch(corpus) == 2
    with pytest.raises(TychoraError):
        corpus.raise_access_floor(ReviewTarget(revision_id=b.revision.revision_id), 1)
    assert epoch(corpus) == 2


def assertion_bundle(lines):
    b = seed_bundle()
    ref = artifact(b"\n".join(lines) + b"\n", "application/x-ndjson")
    return b.model_copy(
        update={
            "artifacts": (*b.artifacts, ref),
            "extraction": b.extraction.model_copy(
                update={"assertions_artifact_sha": ref.sha256}
            ),
        }
    )


VALID_ASSERTION = b'{"type":"assertion","schema_version":"1","record_id":"record-1","assertion_id":"assertion-1"}'


def test_assertion_decision_pins_verified_record_and_advances_epoch(corpus):
    b = assertion_bundle([VALID_ASSERTION])
    corpus.commit_extraction(b)
    sha = b.extraction.assertions_artifact_sha
    first = corpus.record_assertion_decision(
        "assertion-1", sha, "record-1", "rejected", "reviewer"
    )
    second = corpus.record_assertion_decision(
        "assertion-1", sha, "record-1", "confirmed", "reviewer"
    )
    assert second > first
    assert epoch(corpus) == 2
    assert corpus.db.execute(
        "SELECT provenance_artifact_sha,provenance_record_id,state FROM assertion_decisions ORDER BY decision_id"
    ).fetchall() == [(sha, "record-1", "rejected"), (sha, "record-1", "confirmed")]


@pytest.mark.parametrize(
    "case",
    [
        "wrong_record",
        "wrong_assertion",
        "duplicate_record",
        "duplicate_assertion",
        "invalid_json",
        "wrong_version",
        "wrong_type",
        "unregistered",
        "wrong_state",
    ],
)
def test_assertion_decision_rejects_unverified_binding(corpus, case):
    lines = [VALID_ASSERTION]
    if case in ("duplicate_record", "duplicate_assertion"):
        replacement = (
            VALID_ASSERTION.replace(b"assertion-1", b"assertion-2")
            if case == "duplicate_record"
            else VALID_ASSERTION.replace(b"record-1", b"record-2")
        )
        lines.append(replacement)
    elif case == "invalid_json":
        lines = [b"bad-json"]
    elif case == "wrong_version":
        lines = [VALID_ASSERTION.replace(b'"1"', b'"2"')]
    elif case == "wrong_type":
        lines = [VALID_ASSERTION.replace(b'"type":"assertion"', b'"type":"unknown"')]
    b = assertion_bundle(lines)
    corpus.commit_extraction(b)
    sha = (
        b.revision.original_sha
        if case == "unregistered"
        else b.extraction.assertions_artifact_sha
    )
    with pytest.raises(TychoraError):
        corpus.record_assertion_decision(
            "absent" if case == "wrong_assertion" else "assertion-1",
            sha,
            "absent" if case == "wrong_record" else "record-1",
            "invented" if case == "wrong_state" else "confirmed",
            "reviewer",
        )
    assert epoch(corpus) == 0
    assert (
        corpus.db.execute("SELECT COUNT(*) FROM assertion_decisions").fetchone()[0] == 0
    )


def test_document_locator_retains_unit_coordinate_domain(corpus):
    b = seed_bundle()
    loc = DocumentLocator(
        artifact_ref=b.revision.original_sha,
        native_ref=b.source.source_key,
        char_start=0,
        char_end=len(b.units[0].text),
        physical_page=1,
    )
    b = b.model_copy(
        update={
            "units": (b.units[0].model_copy(update={"locators": (loc,)}),),
            "locators": (b.locators[0].model_copy(update={"locator": loc}),),
        }
    )
    corpus.commit_extraction(b)
    hydrated = corpus.hydrate((b.units[0].unit_id,))[0]
    assert hydrated.locators[0].char_domain == "unit"
    assert hydrated.locators[0].model_dump(mode="json")["char_domain"] == "unit"


def test_record_foundations_must_resolve_to_original_units(corpus):
    b = seed_bundle(kind="standard@1")
    bindings = cast(
        Mapping[str, Mapping[str, Mapping[str, str]]],
        registry().artifact["definition_schema_bindings"],
    )
    binding = bindings["kinds"]["standard@1"]["record_schema"]
    data = FrozenJsonMap(
        {
            "requirement": "Retain the source.",
            "target": "record",
            "provenance": [b.source.source_id],
            "status": "recorded",
            "foundation_ids": ["missing-unit"],
        }
    )
    row = StructuredRecordRow(
        unit_id=b.units[0].unit_id,
        record_type="rule",
        schema_hash=cast(str, registry().schema_digests[binding]),
        data_json=data,
    )
    with pytest.raises(TychoraError):
        corpus.commit_extraction(b.model_copy(update={"records": (row,)}))
    assert (
        corpus.db.execute("SELECT COUNT(*) FROM structured_records").fetchone()[0] == 0
    )


def test_verified_records_and_identifiers_persist(corpus):
    from tychora.contracts.corpus import IdentifierRow

    b = seed_bundle(kind="standard@1")
    bindings = cast(
        Mapping[str, Mapping[str, Mapping[str, str]]],
        registry().artifact["definition_schema_bindings"],
    )
    binding = bindings["kinds"]["standard@1"]["record_schema"]
    data = FrozenJsonMap(
        {
            "requirement": "Retain the source.",
            "target": "record",
            "provenance": [b.source.source_id],
            "status": "recorded",
            "foundation_ids": [b.units[0].unit_id],
        }
    )
    row = StructuredRecordRow(
        unit_id=b.units[0].unit_id,
        record_type="rule",
        schema_hash=cast(str, registry().schema_digests[binding]),
        data_json=data,
    )
    b = b.model_copy(
        update={
            "records": (row,),
            "identifiers": (
                IdentifierRow(
                    namespace="standard",
                    normalized_value="STD-1",
                    original_value="STD-1",
                    unit_id=b.units[0].unit_id,
                ),
            ),
        }
    )
    corpus.commit_extraction(b)
    corpus.commit_extraction(b)
    assert corpus.db.execute(
        "SELECT record_type FROM structured_records"
    ).fetchall() == [("rule",)]
    assert corpus.db.execute("SELECT normalized_value FROM identifiers").fetchall() == [
        ("STD-1",)
    ]


def test_failed_registration_rolls_back_epoch_and_access_floor(corpus):
    b = seed_bundle()
    corpus.commit_extraction(b)
    changed = b.model_copy(
        update={
            "source": b.source.model_copy(update={"access_floor": 3}),
            "units": (b.units[0].model_copy(update={"boundary_key": "collision"}),),
        }
    )
    with pytest.raises(TychoraError):
        corpus.commit_extraction(changed)
    assert epoch(corpus) == 0
    assert corpus.db.execute("SELECT access_floor FROM sources").fetchone()[0] == 0


def test_artifact_reads_are_outside_write_transactions(corpus):
    delegate = reader()

    class RecordingReader:
        def open_verified(self, ref: ArtifactRef) -> AbstractContextManager[BinaryIO]:
            assert not corpus.db.in_transaction
            return delegate.open_verified(ref)

        def put_stream(self, stream: BinaryIO, media_type: str) -> ArtifactRef:
            return delegate.put_stream(stream, media_type)

        def stat(self, ref: ArtifactRef) -> ArtifactStat:
            return delegate.stat(ref)

        def pin_payloads(self, shas: frozenset[str]) -> AbstractContextManager[None]:
            return delegate.pin_payloads(shas)

        def payload_guard(self, sha: str) -> AbstractContextManager[None]:
            return delegate.payload_guard(sha)

        def payload_is_pinned(self, sha: str) -> bool:
            return delegate.payload_is_pinned(sha)

    corpus.artifacts = RecordingReader()
    b = assertion_bundle([VALID_ASSERTION])
    corpus.commit_extraction(b)
    corpus.record_assertion_decision(
        "assertion-1",
        b.extraction.assertions_artifact_sha,
        "record-1",
        "confirmed",
        "owner",
    )


def code_bundle():
    text = "import os\n\ndef run():\n    pass\n"
    b = seed_bundle(text, source_key="src/parser.py", kind="code_file@1")

    def loc(start, end, first, last):
        return CodeLocator(
            artifact_ref=b.revision.original_sha,
            native_ref="src/parser.py",
            relative_path="src/parser.py",
            char_start=start,
            char_end=end,
            byte_start=start,
            byte_end=end,
            line_start=first,
            line_end=last,
        )

    import_loc, symbol_loc = loc(0, 9, 1, 1), loc(11, 31, 3, 4)
    source = b.units[0].model_copy(
        update={
            "unit_id": "import",
            "text": "import os",
            "text_sha": sha256(b"import os").hexdigest(),
            "locators": (import_loc,),
        }
    )
    unit = b.units[0].model_copy(
        update={
            "unit_id": "function",
            "unit_type": "symbol",
            "ordinal": 1,
            "text": text[11:],
            "text_sha": sha256(text[11:].encode()).hexdigest(),
            "locators": (symbol_loc,),
        }
    )
    bindings = cast(
        Mapping[str, Mapping[str, Mapping[str, str]]],
        registry().artifact["definition_schema_bindings"],
    )
    binding = bindings["kinds"]["code_file@1"]["record_schema"]
    data = FrozenJsonMap(
        {
            "relative_path": "src/parser.py",
            "language": "python",
            "symbol_kind": "function",
            "name": "run",
            "qualified_name": "run",
            "declaration_span": {
                "char_start": 11,
                "char_end": 21,
                "byte_start": 11,
                "byte_end": 21,
                "line_start": 3,
                "line_end": 3,
                "coordinate_domain": "original_file",
            },
            "parent_unit_id": None,
            "overload_discriminator": None,
            "definition_key": "run@1",
            "adapter_fingerprint": "parser@1",
            "context_spans": [
                {
                    "source_unit_id": "import",
                    "char_start": 0,
                    "char_end": 9,
                    "original_file_locator": {
                        "char_start": 0,
                        "char_end": 9,
                        "byte_start": 0,
                        "byte_end": 9,
                        "line_start": 1,
                        "line_end": 1,
                        "coordinate_domain": "original_file",
                    },
                    "reason": "referenced_import",
                    "dependency_unit_ids": ["import"],
                }
            ],
        }
    )
    row = StructuredRecordRow(
        unit_id="function",
        record_type="code_symbol",
        schema_hash=cast(str, registry().schema_digests[binding]),
        data_json=data,
    )
    return b.model_copy(
        update={
            "units": (source, unit),
            "chunks": (),
            "records": (row,),
            "locators": (
                b.locators[0].model_copy(
                    update={"unit_id": "import", "locator": import_loc}
                ),
                b.locators[0].model_copy(
                    update={
                        "locator_id": "symbol-locator",
                        "unit_id": "function",
                        "locator": symbol_loc,
                    }
                ),
            ),
        }
    )


def test_code_context_records_bind_real_original_ranges(corpus):
    b = code_bundle()
    corpus.commit_extraction(b)
    assert [u.text for u in corpus.hydrate(("import", "function"))] == [
        "import os",
        "def run():\n    pass\n",
    ]


@pytest.mark.parametrize(
    "case",
    [
        "context_missing",
        "context_empty",
        "context_bounds",
        "context_original_bounds",
        "context_dependencies",
        "context_parent",
        "symbol_parent",
        "symbol_path",
        "symbol_declaration",
        "code_byte",
        "code_line",
    ],
)
def test_code_context_and_locator_provenance(corpus, case):
    b = code_bundle()
    import json

    data = json.loads(b.records[0].model_dump_json())["data_json"]
    span = data["context_spans"][0]
    if case == "context_missing":
        span["source_unit_id"] = "absent"
    elif case == "context_empty":
        u = b.units[0].model_copy(
            update={"text": "", "text_sha": sha256(b"").hexdigest()}
        )
        b = b.model_copy(update={"units": (u, b.units[1])})
    elif case == "context_bounds":
        span["char_end"] = 10
    elif case == "context_original_bounds":
        span["original_file_locator"]["char_end"] = 10
    elif case == "context_dependencies":
        span["dependency_unit_ids"] = ["function"]
    elif case == "context_parent":
        span["reason"] = "parent_declaration"
    elif case == "symbol_parent":
        data["parent_unit_id"] = "import"
    elif case == "symbol_path":
        data["relative_path"] = "other.py"
    elif case == "symbol_declaration":
        data["declaration_span"]["byte_end"] = 200
    elif case in ("code_byte", "code_line"):
        loc = (
            b.units[1]
            .locators[0]
            .model_copy(
                update={"byte_end": 32} if case == "code_byte" else {"line_end": 5}
            )
        )
        b = b.model_copy(
            update={
                "units": (
                    b.units[0],
                    b.units[1].model_copy(update={"locators": (loc,)}),
                ),
                "locators": (
                    b.locators[0],
                    b.locators[1].model_copy(update={"locator": loc}),
                ),
            }
        )
    b = b.model_copy(
        update={
            "records": (
                b.records[0].model_copy(update={"data_json": FrozenJsonMap(data)}),
            )
        }
    )
    with pytest.raises(TychoraError):
        corpus.commit_extraction(b)
    assert corpus.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 0


def test_deleted_artifact_cannot_be_resurrected_by_retry(corpus):
    b = seed_bundle()
    corpus.commit_extraction(b)
    corpus.db.execute(
        "UPDATE artifacts SET payload_state='deleted',payload_deleted_at='2024-02-01' WHERE artifact_sha=?",
        (b.revision.original_sha,),
    )
    with pytest.raises(TychoraError):
        corpus.commit_extraction(b)
    assert (
        corpus.db.execute(
            "SELECT payload_state FROM artifacts WHERE artifact_sha=?",
            (b.revision.original_sha,),
        ).fetchone()[0]
        == "deleted"
    )


def test_shared_artifact_keeps_first_registration_timestamp(corpus):
    first = seed_bundle()
    corpus.commit_extraction(first)
    second = seed_bundle(source_key="second.md")
    second = second.model_copy(
        update={
            "extraction": second.extraction.model_copy(
                update={"created_at": "2025-01-01T00:00:00Z"}
            )
        }
    )
    corpus.commit_extraction(second)
    assert corpus.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 2
    assert (
        corpus.db.execute(
            "SELECT created_at FROM artifacts WHERE artifact_sha=?",
            (first.revision.original_sha,),
        ).fetchone()[0]
        == "2024-01-01T00:00:00Z"
    )


def test_immutable_revision_artifacts_cannot_gain_new_bindings(corpus):
    b = seed_bundle()
    corpus.commit_extraction(b)
    changed = b.model_copy(
        update={
            "revision_artifacts": (
                *b.revision_artifacts,
                b.revision_artifacts[0].model_copy(
                    update={"source_ref": "invented.md"}
                ),
            )
        }
    )
    with pytest.raises(TychoraError):
        corpus.commit_extraction(changed)
    assert (
        corpus.db.execute("SELECT COUNT(*) FROM revision_artifacts").fetchone()[0] == 1
    )


def test_caller_transaction_is_rejected_before_external_reads(corpus):
    class RejectReader:
        def open_verified(self, ref):
            pytest.fail("Artifact reader was called inside caller transaction")

    corpus.artifacts = RejectReader()
    corpus.db.execute("BEGIN IMMEDIATE")
    with pytest.raises(TychoraError):
        corpus.commit_extraction(seed_bundle())
    assert corpus.db.in_transaction
    corpus.db.rollback()


def test_hydration_reads_all_units_in_one_short_transaction(corpus):
    b = seed_bundle()
    corpus.commit_extraction(b)
    observed = []
    corpus.db.set_trace_callback(lambda sql: observed.append(sql))
    corpus.hydrate((b.units[0].unit_id, b.units[0].unit_id))
    corpus.db.set_trace_callback(None)
    assert observed[0] == "BEGIN"
    assert observed[-1] == "COMMIT"
    assert not corpus.db.in_transaction


def test_missing_unit_hydration_closes_its_read_transaction(corpus):
    with pytest.raises(TychoraError) as error:
        corpus.hydrate(("absent",))
    assert error.value.code == "NOT_FOUND"
    assert not corpus.db.in_transaction


@pytest.mark.parametrize("format", ["text", "code"])
def test_original_text_must_match_its_verified_locator(corpus, format):
    b = code_bundle() if format == "code" else seed_bundle()
    index = 1 if format == "code" else 0
    text = (
        b.units[index].text.replace("pass", "fake")
        if format == "code"
        else "Invented result!!"
    )
    units = list(b.units)
    units[index] = units[index].model_copy(
        update={"text": text, "text_sha": sha256(text.encode()).hexdigest()}
    )
    b = b.model_copy(update={"units": tuple(units), "chunks": ()})
    with pytest.raises(TychoraError):
        corpus.commit_extraction(b)


@pytest.mark.parametrize("change", ["add", "omit", "change_value"])
def test_new_extraction_cannot_change_revision_facets(corpus, change):
    from tychora.contracts.identity import extraction_id

    b = seed_bundle()
    original_facets = (
        ()
        if change == "add"
        else (
            FacetRow(
                revision_id=b.revision.revision_id,
                facet_key="scope.project",
                value="original",
            ),
        )
    )
    b = b.model_copy(update={"facets": original_facets})
    corpus.commit_extraction(b)
    before = corpus.db.execute(
        "SELECT revision_id,facet_key,ordinal,text_value FROM facet_values"
    ).fetchall()
    facets = (
        ()
        if change == "omit"
        else (
            FacetRow(
                revision_id=b.revision.revision_id,
                facet_key="scope.project",
                value="changed",
            ),
        )
    )
    eid = extraction_id(
        b.revision.revision_id, "parser@2", b.extraction.normalizer_fingerprint
    )
    second = b.model_copy(
        update={
            "extraction": b.extraction.model_copy(
                update={"extraction_id": eid, "parser_fingerprint": "parser@2"}
            ),
            "units": (),
            "locators": (),
            "chunks": (),
            "facets": facets,
        }
    )
    second = type(b).model_validate_json(second.model_dump_json())
    with pytest.raises(TychoraError):
        corpus.commit_extraction(second)
    assert (
        corpus.db.execute(
            "SELECT revision_id,facet_key,ordinal,text_value FROM facet_values"
        ).fetchall()
        == before
    )
    assert corpus.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 1
    assert epoch(corpus) == 0
    assert corpus.commit_extraction(b) == b.extraction.extraction_id


@pytest.mark.parametrize(
    "case",
    [
        "text_bounds",
        "text_binding",
        "code_bytes",
        "code_lines",
        "image_binding",
        "document_domain",
    ],
)
def test_asset_locators_require_verified_source_bounds_and_binding(corpus, case):
    from tychora.contracts.evidence import TextLocator

    b = seed_bundle("αβ\nthird.")
    image = artifact(b"synthetic asset", "image/png")
    loc = TextLocator(
        artifact_ref=b.revision.original_sha,
        native_ref=b.source.source_key,
        char_start=0,
        char_end=len(b.units[0].text),
    )
    if case == "text_bounds":
        loc = loc.model_copy(update={"char_end": 999})
    elif case == "text_binding":
        loc = loc.model_copy(update={"native_ref": "unrelated.txt"})
    elif case in ("code_bytes", "code_lines"):
        loc = CodeLocator(
            artifact_ref=b.revision.original_sha,
            native_ref=b.source.source_key,
            relative_path=b.source.source_key,
            char_start=0,
            char_end=len(b.units[0].text),
            byte_start=0,
            byte_end=999 if case == "code_bytes" else len(b.units[0].text.encode()),
            line_start=1,
            line_end=999 if case == "code_lines" else 2,
        )
    elif case == "image_binding":
        loc = ImageLocator(
            artifact_ref=b.revision.original_sha, native_ref="unrelated-image"
        )
    else:
        loc = DocumentLocator(
            artifact_ref=b.revision.original_sha,
            native_ref=b.source.source_key,
            char_start=0,
            char_end=1,
        )
    asset = AssetRow(
        asset_id="asset",
        extraction_id=b.extraction.extraction_id,
        artifact_sha=image.sha256,
        asset_type="figure",
        native_ref="figure:1",
        locator=loc,
    )
    b = b.model_copy(
        update={
            "units": (),
            "locators": (),
            "chunks": (),
            "artifacts": (*b.artifacts, image),
            "assets": (asset,),
        }
    )
    b = type(b).model_validate_json(b.model_dump_json())
    with pytest.raises(TychoraError):
        corpus.commit_extraction(b)
    assert corpus.db.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    assert corpus.db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0


@pytest.mark.parametrize("format", ["text", "code", "image"])
def test_asset_locator_valid_binding_without_unit_locator(corpus, format):
    from tychora.contracts.evidence import TextLocator

    b = seed_bundle("αβ\nthird.")
    image = artifact(b"synthetic asset", "image/png")
    if format == "text":
        loc = TextLocator(
            artifact_ref=b.revision.original_sha,
            native_ref=b.source.source_key,
            char_start=0,
            char_end=len(b.units[0].text),
        )
    elif format == "code":
        loc = CodeLocator(
            artifact_ref=b.revision.original_sha,
            native_ref=b.source.source_key,
            relative_path=b.source.source_key,
            char_start=0,
            char_end=len(b.units[0].text),
            byte_start=0,
            byte_end=len(b.units[0].text.encode()),
            line_start=1,
            line_end=2,
        )
    else:
        loc = ImageLocator(artifact_ref=image.sha256, native_ref="figure:1")
    asset = AssetRow(
        asset_id="asset",
        extraction_id=b.extraction.extraction_id,
        artifact_sha=image.sha256,
        asset_type="figure",
        native_ref="figure:1",
        locator=loc,
    )
    b = b.model_copy(
        update={
            "units": (),
            "locators": (),
            "chunks": (),
            "artifacts": (*b.artifacts, image),
            "assets": (asset,),
        }
    )
    corpus.commit_extraction(b)
    assert (
        corpus.db.execute("SELECT locator_json FROM assets").fetchone()[0] is not None
    )


@pytest.mark.parametrize("foundation", ["original", "proxy"])
def test_record_foundation_excludes_source_labelled_proxy(corpus, foundation):
    b = proxy_bundle("standard@1")
    b = b.model_copy(
        update={
            "units": (
                b.units[0],
                b.units[1].model_copy(update={"text_origin": "source"}),
            )
        }
    )
    bindings = cast(
        Mapping[str, Mapping[str, Mapping[str, str]]],
        registry().artifact["definition_schema_bindings"],
    )
    schema = bindings["kinds"]["standard@1"]["record_schema"]
    data = FrozenJsonMap(
        {
            "requirement": "Retain the source.",
            "target": "record",
            "provenance": [b.source.source_id],
            "status": "recorded",
            "foundation_ids": [
                b.units[0].unit_id if foundation == "original" else "proxy-1"
            ],
        }
    )
    record = StructuredRecordRow(
        unit_id=b.units[0].unit_id,
        record_type="rule",
        schema_hash=cast(str, registry().schema_digests[schema]),
        data_json=data,
    )
    b = b.model_copy(update={"records": (record,)})
    b = type(b).model_validate_json(b.model_dump_json())
    if foundation == "proxy":
        with pytest.raises(TychoraError):
            corpus.commit_extraction(b)
        assert (
            corpus.db.execute("SELECT COUNT(*) FROM structured_records").fetchone()[0]
            == 0
        )
        assert corpus.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 0
    else:
        corpus.commit_extraction(b)
        assert corpus.db.execute(
            "SELECT text_origin,citable FROM evidence_units WHERE unit_id=?",
            ("proxy-1",),
        ).fetchone() == ("source", 0)


@pytest.mark.parametrize("case", ["context_source", "context_dependency"])
def test_code_context_excludes_source_labelled_proxies(corpus, case):
    import json

    b = code_bundle()
    image = artifact(b"context image", "image/png")
    derivation = artifact(b"context derivation", "application/json")
    asset = AssetRow(
        asset_id="context-asset",
        extraction_id=b.extraction.extraction_id,
        artifact_sha=image.sha256,
        asset_type="figure",
        native_ref="context-image",
        locator=ImageLocator(artifact_ref=image.sha256, native_ref="context-image"),
    )
    if case == "context_source":
        proxy_id = "import"
        units = (b.units[0].model_copy(update={"citable": False}), b.units[1])
    else:
        proxy_id = "context-proxy"
        units = (
            *b.units,
            b.units[0].model_copy(
                update={
                    "unit_id": proxy_id,
                    "ordinal": 2,
                    "citable": False,
                    "locators": (),
                }
            ),
        )
        data = json.loads(b.records[0].model_dump_json())["data_json"]
        data["context_spans"][0]["dependency_unit_ids"].append(proxy_id)
        b = b.model_copy(
            update={
                "records": (
                    b.records[0].model_copy(update={"data_json": FrozenJsonMap(data)}),
                )
            }
        )
    proxy = AssetProxyRow(
        proxy_unit_id=proxy_id,
        asset_id=asset.asset_id,
        origin_type="caption_copy",
        derivation_sha=derivation.sha256,
        required_unit_ids=frozenset({"function"}),
    )
    b = b.model_copy(
        update={
            "units": units,
            "assets": (asset,),
            "proxies": (proxy,),
            "artifacts": (*b.artifacts, image, derivation),
        }
    )
    b = type(b).model_validate_json(b.model_dump_json())
    with pytest.raises(TychoraError):
        corpus.commit_extraction(b)
    assert corpus.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 0


def test_new_extraction_preserves_unchanged_revision_facets(corpus):
    from tychora.contracts.identity import extraction_id

    b = seed_bundle()
    b = b.model_copy(
        update={
            "facets": (
                FacetRow(
                    revision_id=b.revision.revision_id,
                    facet_key="scope.project",
                    value="original",
                ),
            )
        }
    )
    corpus.commit_extraction(b)
    eid = extraction_id(
        b.revision.revision_id, "parser@2", b.extraction.normalizer_fingerprint
    )
    second = b.model_copy(
        update={
            "extraction": b.extraction.model_copy(
                update={"extraction_id": eid, "parser_fingerprint": "parser@2"}
            ),
            "units": (),
            "locators": (),
            "chunks": (),
        }
    )
    assert corpus.commit_extraction(second) == eid
    assert corpus.commit_extraction(b) == b.extraction.extraction_id
    assert corpus.db.execute("SELECT text_value FROM facet_values").fetchall() == [
        ("original",)
    ]
    assert corpus.db.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 2
    assert epoch(corpus) == 0


def test_corpus_keeps_first_physical_artifact_mime_label(corpus):
    first = seed_bundle("Shared actual bytes.", source_key="first.md")
    corpus.commit_extraction(first)
    second = seed_bundle("Shared actual bytes.", source_key="second.md")
    second = second.model_copy(
        update={
            "artifacts": tuple(
                ref.model_copy(update={"media_type": "application/custom"})
                for ref in second.artifacts
            )
        }
    )
    corpus.commit_extraction(second)
    assert corpus.db.execute(
        "SELECT media_type FROM artifacts WHERE artifact_sha=?",
        (first.artifacts[0].sha256,),
    ).fetchone() == (first.artifacts[0].media_type,)
    assert corpus.hydrate((second.units[0].unit_id,)) == second.units
