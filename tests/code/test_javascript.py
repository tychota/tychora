from typing import Any, cast

import pytest

from tychora.adapters.code.typescript import TypeScriptAdapter


@pytest.mark.parametrize(
    ("dialect", "source", "expected_name"),
    [
        ("typescript", b"const load = (value: string) => value;", "load"),
        ("tsx", b"const View = () => <section />;", "View"),
        ("javascript", b"const load = value => value;", "load"),
        ("jsx", b"const View = () => <section />;", "View"),
    ],
)
def test_each_declared_dialect_uses_its_explicit_installed_grammar(
    dialect, source, expected_name
):
    adapter = TypeScriptAdapter(dialect)

    syntax = adapter.parse(source, {})

    assert syntax.coverage == "parsed"
    assert syntax.language == dialect
    assert f":{dialect}@" in syntax.grammar
    assert adapter.capabilities().dialects == (dialect,)
    assert [item.name for item in adapter.definitions(syntax)] == [expected_name]


def test_javascript_definitions_inheritance_and_dynamic_calls_are_explicit():
    source = (
        b"export class Store extends Base {\n"
        b"  load() { obj[key](); return factory()(); }\n"
        b"}\n"
    )
    adapter = TypeScriptAdapter("javascript")

    syntax = adapter.parse(source, {})
    definitions = adapter.definitions(syntax)
    references = adapter.references(syntax)

    assert [item.qualified_name for item in definitions] == ["Store", "Store.load"]
    assert [item.spelling for item in references if item.form == "inheritance"] == [
        "Base"
    ]
    assert [
        (item.spelling, item.supported) for item in references if item.form == "call"
    ] == [
        ("obj[key]", False),
        ("factory()", False),
        ("factory", True),
    ]


@pytest.mark.parametrize("dialect", ["tsx", "jsx"])
def test_jsx_nodes_and_component_import_context_are_preserved(dialect):
    annotation = b": {name: string}" if dialect == "tsx" else b""
    source = (
        b'import Panel from "./Panel";\n'
        b"export default function View(props"
        + annotation
        + b") { return <Panel name={props.name} />; }"
    )
    adapter = TypeScriptAdapter(dialect)

    syntax = adapter.parse(source, {})
    view = next(item for item in adapter.definitions(syntax) if item.name == "View")
    jsx = [item for item in adapter.references(syntax) if item.form == "jsx"]

    assert [(item.spelling, item.supported) for item in jsx] == [("Panel", True)]
    assert jsx[0].source_scope_ref == view.native_ref
    assert [item.reason for item in adapter.context_spans(view)] == [
        "referenced_import"
    ]


def test_capabilities_state_precise_static_analysis_limits():
    capabilities = TypeScriptAdapter("jsx").capabilities()

    assert capabilities.declaration_forms == (
        "class",
        "function",
        "method",
        "arrow_function",
    )
    assert TypeScriptAdapter("typescript").capabilities().declaration_forms == (
        "class",
        "interface",
        "function",
        "function_signature",
        "method",
        "method_signature",
        "arrow_function",
    )
    assert capabilities.reference_forms == (
        "import",
        "export",
        "call",
        "inheritance",
        "jsx",
    )
    assert capabilities.limitations == (
        "dynamic calls are retained as unsupported references",
        "computed property definitions and their bodies are excluded from static scope extraction",
        "JSX element names are syntactic candidates rather than resolved runtime bindings",
        "decorators are reported only when the selected grammar exposes structural decorator nodes",
        "grammar-recovered syntax is diagnostic and excluded from structural references",
    )

    with pytest.raises(ValueError, match="Unsupported TypeScript dialect"):
        TypeScriptAdapter(cast(Any, "flow"))


def test_recovered_method_does_not_invent_references_from_its_error_region():
    source = (
        b"class Store {\n"
        b"  good() { return okay(); }\n"
        b"  broken(,) { missing(); }\n"
        b"  later() { return 2; }\n"
        b"}\n"
    )
    adapter = TypeScriptAdapter("javascript")

    syntax = adapter.parse(source, {})

    assert syntax.coverage == "partial"
    assert [item.qualified_name for item in adapter.definitions(syntax)] == [
        "Store",
        "Store.good",
        "Store.later",
    ]
    assert [
        item.spelling for item in adapter.references(syntax) if item.form == "call"
    ] == ["okay"]
