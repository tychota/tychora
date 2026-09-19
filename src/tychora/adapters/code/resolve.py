"""Deterministic bounded static reference resolution."""

from collections import defaultdict
from collections.abc import Mapping

from tychora.contracts.code import Resolution, ResolutionRate, SymbolCatalog
from tychora.contracts.identity import digest
from tychora.ports.language import Reference

_CANDIDATE_LIMIT = 20


def _reference_id(reference: Reference, catalog: SymbolCatalog) -> str:
    return digest(
        "code-reference-v1",
        catalog.source_id,
        catalog.revision_id,
        catalog.source_relative_path,
        catalog.source_language,
        reference.native_ref,
        reference.source_scope_ref,
        reference.form,
        reference.byte_start,
        reference.byte_end,
    )


def _finish(
    reference: Reference,
    catalog: SymbolCatalog,
    candidate_ids: tuple[str, ...] | list[str],
    rule: str,
    reasons: tuple[str, ...],
) -> Resolution:
    unique = tuple(sorted(set(candidate_ids)))
    state = "resolved" if len(unique) == 1 else "ambiguous" if unique else "unresolved"
    selected = unique[0] if state == "resolved" else None
    return Resolution(
        reference_id=_reference_id(reference, catalog),
        source_id=catalog.source_id,
        revision_id=catalog.revision_id,
        relative_path=catalog.source_relative_path,
        language=catalog.source_language,
        reference_form=reference.form,
        spelling=reference.spelling,
        owning_scope=reference.source_scope,
        source_scope_ref=reference.source_scope_ref,
        byte_start=reference.byte_start,
        byte_end=reference.byte_end,
        char_start=reference.char_start,
        char_end=reference.char_end,
        line_start=reference.line_start,
        line_end=reference.line_end,
        adapter_fingerprint=catalog.source_adapter_fingerprint,
        supported=reference.supported,
        state=state,
        selected_target=selected,
        candidates=unique[:_CANDIDATE_LIMIT],
        rule=rule,
        reasons=reasons,
        truncated=len(unique) > _CANDIDATE_LIMIT,
        total_candidate_count=len(unique),
    )


def _resolve_one(reference: Reference, catalog: SymbolCatalog) -> Resolution:
    qualified = {
        name for name in (*reference.candidate_names, reference.spelling) if "." in name
    }
    exact = [
        item.target_id
        for item in catalog.definitions
        if item.qualified_name in qualified
    ]
    if exact:
        return _finish(
            reference,
            catalog,
            exact,
            "exact_qualified_target",
            ("qualified name matched exactly",),
        )

    mapped_revisions = {
        item.target_revision_id for item in catalog.revision_dependencies
    }
    imported = []
    for binding in catalog.imports:
        if (
            binding.relative_path != catalog.source_relative_path
            or binding.local_name
            not in {
                reference.local_name,
                reference.spelling,
                *reference.candidate_names,
            }
            or binding.source_scope_ref not in (None, reference.source_scope_ref)
            or binding.target_revision_id != catalog.revision_id
            and binding.target_revision_id not in mapped_revisions
        ):
            continue
        imported.extend(binding.target_ids)
    if imported:
        return _finish(
            reference,
            catalog,
            imported,
            "explicit_import_binding",
            ("visible import binding matched local spelling",),
        )

    short_names = {reference.spelling.rsplit(".", 1)[-1]}
    short_names.update(name.rsplit(".", 1)[-1] for name in reference.candidate_names)
    same_file = [
        item
        for item in catalog.definitions
        if item.relative_path == catalog.source_relative_path
        and item.language == catalog.source_language
        and item.name in short_names
    ]
    scoped = [
        item.target_id
        for item in same_file
        if item.parent_native_ref == reference.source_scope_ref
    ]
    candidates = scoped or [
        item.target_id for item in same_file if item.parent_native_ref is None
    ]
    if candidates:
        return _finish(
            reference,
            catalog,
            candidates,
            "unique_same_module_scope",
            ("same file and native lexical scope matched",),
        )
    return _finish(
        reference,
        catalog,
        (),
        "no_static_target",
        ("no qualified, import-bound, or same-module target",),
    )


def resolve(
    references: tuple[Reference, ...], catalog: SymbolCatalog
) -> tuple[Resolution, ...]:
    """Resolve raw references through one explicit captured-file catalog view."""
    return tuple(_resolve_one(reference, catalog) for reference in references)


def resolution_rates(
    resolutions: tuple[Resolution, ...],
    declared_groups: tuple[tuple[str, str], ...] = (),
) -> Mapping[tuple[str, str], ResolutionRate]:
    """Report extracted-reference rates; unsupported syntax is a separate count."""
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"resolved": 0, "ambiguous": 0, "unresolved": 0}
    )
    for group in declared_groups:
        counts[group]
    for item in resolutions:
        if item.supported:
            counts[(item.language, item.reference_form)][item.state] += 1
    result = {}
    for (language, form), values in sorted(counts.items()):
        extracted = sum(values.values())
        result[(language, form)] = ResolutionRate(
            language=language,
            reference_form=form,
            extracted=extracted,
            resolved=values["resolved"],
            ambiguous=values["ambiguous"],
            unresolved=values["unresolved"],
            resolved_rate=values["resolved"] / extracted if extracted else None,
            ambiguous_rate=values["ambiguous"] / extracted if extracted else None,
            unresolved_rate=values["unresolved"] / extracted if extracted else None,
        )
    return result
