"""Per-candidate slices: neighbours by boundary key and persisted ownership."""

import pytest

from tests.helpers.corpus import add_manual
from tests.helpers.eligibility import revision_attempt, revision_view
from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import on_owner, owner_row, owner_rows
from tychora import Tychora
from tychora.application.reranking import prepare_rerank_facts
from tychora.configuration.resolve import resolve_config
from tychora.contracts.candidates import ChannelCandidate
from tychora.contracts.errors import TychoraError
from tychora.contracts.operations import ExtractionContentRequest
from tychora.indexing.representations import prepare_extraction
from tychora.ports.execution import ExecutionContext
from tychora.retrieval.admission import Admission
from tychora.retrieval.fusion import FusedCandidate
from tychora.retrieval.rerank import RerankFacts, matches_from_channels, ranking_input


class CharacterCounter:
    model = "rerank-2.5"
    tokenizer_id = "synthetic-character-counter"

    def count(self, text: str) -> int:
        return len(text)


# Two paragraphs past radius 1 of "Inspect", so a radius-1 neighbourhood is a
# strict part of its boundary.
_MANUAL = (
    "# Pumps\n\nCheck the housing.\n\nInspect seals quarterly.\n\n"
    "Record the result.\n\nDrain the casing.\n\nRefill the reservoir.\n\n"
    "# Valves\n\nReplace worn valves.\n"
)


@pytest.fixture
def kb(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as opened:
        add_manual(opened, _MANUAL, title="Manual")
        yield opened


def _inspect_row(owner, columns):
    """The given columns of the "Inspect" paragraph's unit."""
    row = owner_row(
        owner, f"SELECT {columns} FROM evidence_units WHERE text LIKE 'Inspect%'"
    )
    assert row is not None
    return row


def _read(kb, call):
    owner = kb._workspace.owner
    return kb._workspace.bridge.call(owner.run_read(call, ExecutionContext()))


def test_neighbours_stay_within_boundary_key(kb):
    owner = kb._workspace.owner
    rows = owner_rows(
        owner,
        "SELECT unit_id,boundary_key,ordinal FROM evidence_units ORDER BY ordinal",
    )
    unit_id, boundary, ordinal = _inspect_row(owner, "unit_id,boundary_key,ordinal")
    near = _read(kb, lambda tx: tx.representations.neighbours(unit_id, 1))
    every = _read(kb, lambda tx: tx.representations.neighbours(unit_id, None))
    assert all(row.boundary_key == boundary for row in near + every)
    assert all(abs(row.ordinal - ordinal) <= 1 for row in near)
    assert {row.unit_id for row in near} < {row.unit_id for row in every}
    assert {r[0] for r in rows if r[1] == boundary} == {row.unit_id for row in every}


def test_slice_ownership_equals_prepared_extraction(kb):
    owner = kb._workspace.owner
    extraction_id, unit_id = _inspect_row(owner, "extraction_id,unit_id")
    piece = _read(
        kb, lambda tx: tx.representations.slice(extraction_id, unit_ids=(unit_id,))
    )
    full = on_owner(owner, lambda: prepare_extraction(_full_rows(owner, extraction_id)))
    assert piece.prepared.representation(unit_id) == full.representation(unit_id)
    assert [u.unit_id for u in piece.rows.units] == [unit_id]


def _full_rows(owner, extraction_id):
    """Runs on the writer thread (called inside `on_owner`)."""
    from tychora.adapters.sqlite.repositories.slices import retained_rows
    from tychora.adapters.sqlite.retained_bindings import BindingScope

    revision_id = owner.db.execute(
        "SELECT revision_id FROM extractions WHERE extraction_id=?", (extraction_id,)
    ).fetchone()[0]
    return retained_rows(owner.db, BindingScope(extraction_id, revision_id))


def test_slice_without_interval_sets_is_refused(kb):
    owner = kb._workspace.owner
    extraction_id, unit_id = _inspect_row(owner, "extraction_id,unit_id")
    owner_rows(
        owner,
        "DELETE FROM representation_dependencies WHERE owner_id IN "
        "(SELECT dependency_digest FROM ownership_intervals WHERE unit_id=?)",
        (unit_id,),
    )
    with pytest.raises(TychoraError) as error:
        _read(
            kb, lambda tx: tx.representations.slice(extraction_id, unit_ids=(unit_id,))
        )
    assert error.value.reason == "canonical.facts_missing"


def test_no_read_path_calls_prepare_extraction():
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "src" / "tychora"
    allowed = {
        "indexing/coordinator.py",
        "indexing/representations.py",
        "application/maintenance.py",
    }
    offenders = []
    for path in [
        *source.joinpath("application").rglob("*.py"),
        *source.joinpath("retrieval").rglob("*.py"),
    ]:
        relative = str(path.relative_to(source))
        if relative in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Name) and node.id == "prepare_extraction":
                offenders.append(relative)
    assert offenders == []


def test_inspecting_a_unit_reads_one_slice(kb, monkeypatch):
    from tychora.adapters.sqlite.repositories import SqliteRepresentationRepository

    owner = kb._workspace.owner
    (unit_id,) = _inspect_row(owner, "unit_id")
    calls = []
    original = SqliteRepresentationRepository.slice

    def counted(self, extraction_id, **kwargs):
        calls.append(kwargs)
        return original(self, extraction_id, **kwargs)

    monkeypatch.setattr(SqliteRepresentationRepository, "slice", counted)
    result = kb.inspect(
        {
            "subject": "evidence",
            "id": unit_id,
            "range": {"char_start": 0, "char_end": 7},
        }
    )
    assert result.excerpts[0].text == "Inspect"
    assert calls == [{"unit_ids": (unit_id,)}]


def test_retained_content_pages_without_whole_extraction_reads(kb, monkeypatch):
    from tychora.adapters.sqlite.repositories import representations

    owner = kb._workspace.owner
    lineage = owner_row(
        owner,
        "SELECT r.source_id,e.revision_id,e.extraction_id FROM extractions e "
        "JOIN revisions r USING(revision_id)",
    )
    assert lineage is not None
    source_id, revision_id, extraction_id = lineage
    scopes = []
    original = representations.retained_rows

    def recording(db, scope):
        scopes.append(scope)
        return original(db, scope)

    monkeypatch.setattr(representations, "retained_rows", recording)
    request = ExtractionContentRequest(
        source_id=source_id,
        revision_id=revision_id,
        extraction_id=extraction_id,
        limit=2,
    )
    page = kb._workspace.bridge.call(
        kb._workspace.services.inspection.extraction_windows(
            request, kb.scope, ExecutionContext()
        )
    )
    assert page.windows
    assert scopes and all(scope.unit_ids is not None for scope in scopes)


def test_narrow_rows_cost_less_than_unit_rows(kb):
    from tychora.adapters.sqlite.retained_bindings import ROW_OVERHEAD

    owner = kb._workspace.owner
    extraction_id, unit_id = _inspect_row(owner, "extraction_id,unit_id")
    piece = _read(
        kb, lambda tx: tx.representations.slice(extraction_id, unit_ids=(unit_id,))
    )
    assert ROW_OVERHEAD["artifacts"] <= 256 < ROW_OVERHEAD["evidence_units"]
    assert piece.footprint.row_bytes < piece.footprint.rows * 1024


_FIRST_FILE = (
    "import os\n\n\nclass Box:\n    def run(self):\n        return os.sep\n\n\n"
    "def helper():\n    return 1\n"
)
_SECOND_FILE = "def other():\n    return 2\n\n\ndef another():\n    return 3\n"


@pytest.fixture
def repo_kb(tmp_path):
    from tests.code.conftest import git

    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Synthetic Author")
    git(root, "config", "user.email", "synthetic@example.invalid")
    (root / "first.py").write_text(_FIRST_FILE)
    (root / "second.py").write_text(_SECOND_FILE)
    git(root, "add", "first.py", "second.py")
    git(root, "commit", "-qm", "synthetic fixture")
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.extraction]\nparser="code"\n'
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
    )
    config = resolve_config(settings, "test", {})
    source = {
        "type": "repository",
        "path": str(root),
        "source_key": "code",
        "collection_id": "default",
        "kind": "code_file@1",
        "metadata": {"title": "Synthetic repository"},
    }
    with Tychora.open(
        tmp_path / "ws", config=config, token_counter=OutputCounter()
    ) as opened:
        assert opened.jobs.wait(opened.sources.add(source).id).state == "succeeded"
        yield opened


def _code_unit(owner, relative_path):
    """A symbol unit of one file and its extraction."""
    row = owner_row(
        owner,
        "SELECT u.extraction_id,u.unit_id FROM evidence_units u JOIN locators c "
        "USING(unit_id) WHERE c.locator_type='code' AND c.source_ref=? "
        "AND u.unit_type='symbol' ORDER BY u.ordinal LIMIT 1",
        (relative_path,),
    )
    assert row is not None
    return row


def test_code_unit_slice_reads_only_its_file_binding(repo_kb):
    owner = repo_kb._workspace.owner
    extraction_id, unit_id = _code_unit(owner, "first.py")
    piece = _read(
        repo_kb,
        lambda tx: tx.representations.slice(extraction_id, unit_ids=(unit_id,)),
    )
    files = [
        row.source_ref
        for row in piece.rows.revision_artifacts
        if row.role == "source_file"
    ]
    assert files == ["first.py"]
    whole = _read(
        repo_kb,
        lambda tx: tx.representations.slice(extraction_id, relative_path="second.py"),
    )
    assert [
        r.source_ref for r in whole.rows.revision_artifacts if r.role == "source_file"
    ] == ["second.py"]


@pytest.mark.parametrize("measure", ["units", "rows", "text_bytes", "row_bytes"])
def test_file_slice_over_its_bounds_is_refused(repo_kb, monkeypatch, measure):
    from tychora.adapters.sqlite.repositories import slices

    owner = repo_kb._workspace.owner
    extraction_id, _ = _code_unit(owner, "first.py")
    whole = _read(
        repo_kb,
        lambda tx: tx.representations.slice(extraction_id, relative_path="first.py"),
    )
    assert getattr(whole.footprint, measure) > 1
    monkeypatch.setattr(
        slices, "MAX_SLICE", slices.MAX_SLICE.model_copy(update={measure: 1})
    )
    with pytest.raises(TychoraError) as error:
        _read(
            repo_kb,
            lambda tx: tx.representations.slice(
                extraction_id, relative_path="first.py"
            ),
        )
    assert error.value.reason == "request.limit_exceeded"


def test_many_small_slices_together_exceed_the_operation_bound(kb, monkeypatch):
    from tychora.application import reranking

    candidates, channels, view = _candidates(kb)
    limit = reranking.MAX_OPERATION.model_copy(update={"units": 6})
    monkeypatch.setattr(reranking, "MAX_OPERATION", limit)
    # Each slice alone fits: the refusal comes from their sum.
    for candidate in candidates:
        _sliced_facts(kb, (candidate,), channels, view)
    with pytest.raises(TychoraError) as error:
        _sliced_facts(kb, candidates, channels, view)
    assert error.value.reason == "request.limit_exceeded"


def test_slice_refuses_an_empty_or_unknown_request(kb):
    owner = kb._workspace.owner
    extraction_id, unit_id = _inspect_row(owner, "extraction_id,unit_id")
    with pytest.raises(TychoraError) as empty:
        _read(kb, lambda tx: tx.representations.slice(extraction_id))
    assert empty.value.reason == "invariant.unexpected"
    with pytest.raises(TychoraError) as unknown:
        _read(
            kb,
            lambda tx: tx.representations.slice(
                extraction_id, unit_ids=(unit_id, "absent-unit")
            ),
        )
    assert unknown.value.reason == "canonical.row_invalid"
    with pytest.raises(TychoraError) as mixed:
        _read(
            kb,
            lambda tx: tx.representations.slice(
                extraction_id, unit_ids=(unit_id,), asset_ids=("some-asset",)
            ),
        )
    assert mixed.value.reason == "invariant.unexpected"
    with pytest.raises(TychoraError) as absent:
        _read(
            kb,
            lambda tx: tx.representations.slice(
                extraction_id, asset_ids=("absent-asset",)
            ),
        )
    assert absent.value.reason == "canonical.row_invalid"


def _candidates(kb):
    """Every unit with windows, as a lexical candidate, with its extraction grants."""
    owner = kb._workspace.owner
    windows: dict[str, list[str]] = {}
    grants: dict[str, str] = {}
    for unit_id, chunk_id, extraction_id, revision_id in owner_rows(
        owner,
        "SELECT c.unit_id,c.chunk_id,u.extraction_id,e.revision_id FROM chunks c "
        "JOIN evidence_units u USING(unit_id) JOIN extractions e "
        "ON e.extraction_id=u.extraction_id ORDER BY u.ordinal,c.char_start",
    ):
        windows.setdefault(unit_id, []).append(chunk_id)
        grants[revision_id] = extraction_id
    candidates = tuple(
        FusedCandidate(
            ("unit", unit_id),
            rank,
            1.0 / rank,
            {"lexical": rank},
            matching_window_ids=tuple(ids),
        )
        for rank, (unit_id, ids) in enumerate(windows.items(), 1)
    )
    channels = {
        "lexical": tuple(
            ChannelCandidate(
                key=candidate.key,
                rank=candidate.rank,
                matching_window_ids=candidate.matching_window_ids,
            )
            for candidate in candidates
        )
    }
    return candidates, channels, revision_view(grants)


def _sliced_facts(kb, candidates, channels, view, paths=()):
    owner = kb._workspace.owner
    admission = Admission(candidates, (), {}, {}, (), {}, 64 - len(candidates))
    attempt = revision_attempt(view, owner=owner)
    return kb._workspace.bridge.call(
        prepare_rerank_facts(
            owner, attempt, admission, channels, ExecutionContext(), paths=paths
        )
    )


def _full_facts(kb, candidate, channels, view):
    """The candidate's facts from its whole extraction, prepared from scratch."""
    owner = kb._workspace.owner
    found = owner_row(
        owner,
        "SELECT extraction_id FROM evidence_units WHERE unit_id=?",
        (candidate.key[1],),
    )
    assert found is not None
    (extraction_id,) = found
    rows = on_owner(owner, lambda: _full_rows(owner, extraction_id))
    prepared = prepare_extraction(rows)
    windows = candidate.matching_window_ids
    chunks = _read(
        kb, lambda tx: {row.chunk_id: row for row in tx.representations.chunks(windows)}
    )
    dependencies = _read(
        kb,
        lambda tx: tx.representations.dependencies(
            (*(unit.unit_id for unit in rows.units), *windows)
        ),
    )
    return RerankFacts(
        rows,
        prepared,
        matches_from_channels(
            candidate, channels, chunks, prepared, view, dependencies
        ),
        dependencies=dependencies,
    )


def _assert_parity(kb):
    candidates, channels, view = _candidates(kb)
    assert len(candidates) > 1
    sliced = _sliced_facts(kb, candidates, channels, view)
    for candidate in candidates:
        piece = sliced[candidate.key]
        full = _full_facts(kb, candidate, channels, view)
        for rep in piece.prepared.units:
            assert rep == full.prepared.representation(rep.unit.unit_id)
        assert piece.matches == full.matches
        spans = []
        for facts in (piece, full):
            units = {unit.unit_id: unit for unit in facts.rows.units}
            item = ranking_input(
                candidate, units, view, CharacterCounter(), facts=facts
            )
            spans.append(item.spans)
        assert spans[0] == spans[1]


def test_rerank_facts_from_slices_equal_whole_extraction_facts_for_a_manual(kb):
    _assert_parity(kb)


def test_rerank_facts_from_slices_equal_whole_extraction_facts_for_code(repo_kb):
    _assert_parity(repo_kb)


def test_rerank_facts_refuse_a_window_without_a_persisted_set(kb):
    owner = kb._workspace.owner
    candidates, channels, view = _candidates(kb)
    candidate = candidates[0]
    owner_rows(
        owner,
        "DELETE FROM representation_dependencies WHERE owner_id=?",
        (candidate.matching_window_ids[0],),
    )
    with pytest.raises(TychoraError) as error:
        _sliced_facts(kb, (candidate,), channels, view)
    assert error.value.reason == "canonical.facts_missing"


def test_missing_continuation_target_is_an_integrity_failure(kb):
    from tychora.contracts.graph import GraphPath

    candidates, channels, view = _candidates(kb)
    unit_id = candidates[0].key[1]
    path = GraphPath(
        assertion_ids=("a" * 64,),
        required_support_ids=frozenset({unit_id, "absent-unit"}),
        predicate="continues_table@1",
        status="recorded",
        confidence=0.9,
        source_unit_id=unit_id,
        target_unit_id="absent-unit",
    )
    with pytest.raises(TychoraError) as error:
        _sliced_facts(kb, candidates[:1], channels, view, (path,))
    assert error.value.code == "INTEGRITY_ERROR"
    assert error.value.reason == "canonical.row_invalid"
