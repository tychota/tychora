import pytest

from tychora.adapters.code.normalize import definition_key
from tychora.adapters.code.resolve import resolution_rates, resolve
from tychora.contracts.code import (
    CatalogDefinition,
    ImportBinding,
    OwnedRange,
    RevisionDependency,
    SymbolCatalog,
)
from tychora.ports.language import Reference


def reference(
    spelling: str,
    *,
    native_ref: str = "call:0:4",
    scope_ref: str | None = None,
    candidates: tuple[str, ...] = (),
) -> Reference:
    return Reference(
        form="call",
        spelling=spelling,
        native_ref=native_ref,
        source_scope=(),
        source_scope_ref=scope_ref,
        byte_start=0,
        byte_end=4,
        char_start=0,
        char_end=4,
        line_start=1,
        line_end=1,
        candidate_names=candidates,
    )


def definition(
    target_id: str,
    name: str,
    qualified_name: str,
    *,
    path: str = "src/main.py",
    revision: str = "r1",
    parent_ref: str | None = None,
) -> CatalogDefinition:
    return CatalogDefinition(
        target_id=target_id,
        revision_id=revision,
        relative_path=path,
        language="python",
        name=name,
        qualified_name=qualified_name,
        symbol_kind="function",
        native_ref=f"definition:{target_id}",
        parent_native_ref=parent_ref,
    )


def catalog(*definitions: CatalogDefinition, path: str = "src/main.py", **updates):
    return SymbolCatalog(
        source_id="repository",
        revision_id="r1",
        source_relative_path=path,
        source_language="python",
        source_adapter_fingerprint="a" * 64,
        source_container_id=f"container:{path}",
        definitions=definitions,
        **updates,
    )


def test_definition_identity_includes_file_and_revision():
    def key(rev, path):
        return definition_key(rev, path, "python", "validate", "function", 0)

    assert key("r1", "a.py") != key("r1", "b.py")
    assert key("r1", "a.py") != key("r2", "a.py")
    assert (
        key("r1", "a.py")
        == "9c21f99afdbc5e36084a1d61ab992e3a7efbbf98b48b47680ff5f4dc5e1c1614"
    )
    assert key("r1", "src/./a.py") == key("r1", "src/a.py")
    with pytest.raises(ValueError):
        key("r1", "../a.py")


def test_resolution_prefers_exact_qualified_name_over_short_name_collisions():
    result = resolve(
        (reference("load", candidates=("pkg.load",)),),
        catalog(
            definition("expected", "load", "pkg.load", path="src/pkg.py"),
            definition("other", "load", "other.load", path="src/other.py"),
        ),
    )[0]
    assert (result.state, result.selected_target, result.rule) == (
        "resolved",
        "expected",
        "exact_qualified_target",
    )


def test_local_import_binding_uses_native_scope_identity():
    imports = (
        ImportBinding(
            relative_path="src/main.py",
            source_scope_ref="definition:first",
            local_name="load",
            target_revision_id="r1",
            target_ids=("first-target",),
        ),
        ImportBinding(
            relative_path="src/main.py",
            source_scope_ref="definition:second",
            local_name="load",
            target_revision_id="r1",
            target_ids=("second-target",),
        ),
    )
    first, second = resolve(
        (
            reference("load", native_ref="same", scope_ref="definition:first"),
            reference("load", native_ref="same", scope_ref="definition:second"),
        ),
        catalog(imports=imports),
    )
    assert first.selected_target == "first-target"
    assert second.selected_target == "second-target"
    assert first.reference_id != second.reference_id


def test_ambiguous_alternatives_are_sorted_capped_and_never_selected():
    definitions = tuple(
        definition(f"target-{index:02}", "run", "run") for index in range(25)
    )
    result = resolve((reference("run"),), catalog(*definitions))[0]
    assert result.state == "ambiguous"
    assert result.selected_target is None
    assert result.candidates == tuple(f"target-{index:02}" for index in range(20))
    assert result.total_candidate_count == 25
    assert result.truncated is True


def test_external_import_is_unresolved_without_explicit_revision_mapping():
    binding = ImportBinding(
        relative_path="src/main.py",
        source_scope_ref=None,
        local_name="remote",
        target_revision_id="external-r2",
        target_ids=("remote-target",),
    )
    unresolved = resolve((reference("remote"),), catalog(imports=(binding,)))[0]
    mapped = resolve(
        (reference("remote"),),
        catalog(
            imports=(binding,),
            revision_dependencies=(
                RevisionDependency(
                    source_revision_id="r1", target_revision_id="external-r2"
                ),
            ),
        ),
    )[0]
    assert (unresolved.state, unresolved.selected_target) == ("unresolved", None)
    assert (mapped.state, mapped.selected_target) == ("resolved", "remote-target")


def test_same_native_reference_in_two_files_uses_explicit_file_view():
    definitions = (
        definition("a-target", "run", "run", path="a.py"),
        definition("b-target", "run", "run", path="b.py"),
    )
    ref = reference("run", native_ref="call:0:3")
    a = resolve((ref,), catalog(*definitions, path="a.py"))[0]
    b = resolve((ref,), catalog(*definitions, path="b.py"))[0]
    assert a.selected_target == "a-target"
    assert b.selected_target == "b-target"
    assert a.reference_id != b.reference_id


def test_resolution_rates_exclude_unsupported_and_empty_groups_are_na():
    supported = resolve((reference("missing"),), catalog())[0]
    unsupported_ref = reference("dynamic").model_copy(update={"supported": False})
    unsupported = resolve((unsupported_ref,), catalog())[0]
    rates = resolution_rates(
        (supported, unsupported),
        declared_groups=(("python", "call"), ("python", "inheritance")),
    )
    assert rates[("python", "call")].extracted == 1
    assert rates[("python", "call")].unresolved_rate == 1.0
    assert rates[("python", "inheritance")].extracted == 0
    assert rates[("python", "inheritance")].resolved_rate is None


def test_unsupported_reference_with_unique_target_stays_diagnostic_only():
    unsupported = reference("run").model_copy(update={"supported": False})
    result = resolve(
        (unsupported,),
        catalog(definition("only-target", "run", "run")),
    )[0]
    assert result.supported is False
    assert (result.state, result.selected_target, result.candidates) == (
        "unresolved",
        None,
        (),
    )
    assert result.rule == "unsupported_reference"


def test_resolution_records_smallest_canonical_source_owner():
    source_ranges = (
        OwnedRange(
            owner_unit_id="outer-unit",
            artifact_ref="a" * 64,
            relative_path="src/main.py",
            char_start=0,
            char_end=20,
            byte_start=0,
            byte_end=20,
        ),
        OwnedRange(
            owner_unit_id="inner-unit",
            artifact_ref="a" * 64,
            relative_path="src/main.py",
            char_start=0,
            char_end=8,
            byte_start=0,
            byte_end=8,
        ),
    )
    result = resolve(
        (reference("missing"),), catalog(source_ranges=source_ranges)
    )[0]
    assert result.source_unit_id == "inner-unit"


def test_unique_resolution_is_a_diagnostic_not_gold_correctness():
    result = resolve(
        (reference("run"),),
        catalog(definition("deterministic-target", "run", "run")),
    )[0]
    gold_target = "different-gold-target"
    assert result.state == "resolved"
    assert result.selected_target != gold_target
