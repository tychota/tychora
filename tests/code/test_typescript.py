from tychora.adapters.code.typescript import TypeScriptAdapter


def test_exported_method_has_class_and_export_context():
    source = b"export class Store { async load(): Promise<number> { return 1; } }"
    adapter = TypeScriptAdapter("typescript")

    definitions = adapter.definitions(adapter.parse(source, {}))

    store = next(item for item in definitions if item.qualified_name == "Store")
    method = next(item for item in definitions if item.qualified_name == "Store.load")
    assert source[method.byte_start : method.byte_end].startswith(b"async load")
    assert method.parent_name == "Store"
    assert method.parent_native_ref == store.native_ref
    assert store.export_span is not None
    assert source[store.export_span.byte_start : store.export_span.byte_end] == source
    context = adapter.context_spans(method)
    assert [item.reason for item in context] == ["parent_declaration"]
    assert source[context[0].byte_start : context[0].byte_end] == b"class Store"


def test_overloads_and_interface_members_keep_distinct_native_identity():
    source = (
        b"export function lookup(x: string): string;\n"
        b"export function lookup(x: number): number;\n"
        b"export function lookup(x: string | number) { return x; }\n"
        b"export interface Store extends Base { load(x: string): Promise<number>; }"
    )
    adapter = TypeScriptAdapter("typescript")

    definitions = adapter.definitions(adapter.parse(source, {}))

    overloads = [item for item in definitions if item.qualified_name == "lookup"]
    assert [item.kind for item in overloads] == [
        "function_signature",
        "function_signature",
        "function",
    ]
    assert len({item.native_ref for item in overloads}) == 3
    assert len({item.overload_discriminator for item in overloads}) == 3
    interface = next(item for item in definitions if item.qualified_name == "Store")
    member = next(item for item in definitions if item.qualified_name == "Store.load")
    assert interface.kind == "interface"
    assert member.kind == "method_signature"
    assert member.parent_native_ref == interface.native_ref


def test_structural_decorators_are_exact_and_computed_method_is_not_a_symbol():
    source = (
        b"@sealed\nexport class Store { "
        b"@logged async load() { return 1; } [key]() { hidden(); } #private() {} }"
    )
    adapter = TypeScriptAdapter("typescript")

    definitions = adapter.definitions(adapter.parse(source, {}))

    store = next(item for item in definitions if item.qualified_name == "Store")
    load = next(item for item in definitions if item.qualified_name == "Store.load")
    private = next(
        item for item in definitions if item.qualified_name == "Store.#private"
    )
    assert store.decorator_span is not None
    assert source[store.decorator_span.byte_start : store.decorator_span.byte_end] == (
        b"@sealed"
    )
    assert load.decorator_span is not None
    assert source[load.byte_start : load.byte_end].startswith(b"@logged")
    assert source[load.decorator_span.byte_start : load.decorator_span.byte_end] == (
        b"@logged"
    )
    assert private.name == "#private"
    assert all(item.name != "[key]" for item in definitions)
    assert all(
        item.spelling != "hidden"
        for item in adapter.references(adapter.parse(source, {}))
    )


def test_named_arrow_and_anonymous_callback_keep_nested_lexical_identity():
    source = (
        "const café = async value => value;\n"
        "export const collect = () => [1].map(item => item);\n"
    ).encode()
    adapter = TypeScriptAdapter("typescript")

    syntax = adapter.parse(source, {})
    definitions = adapter.definitions(syntax)

    cafe = next(item for item in definitions if item.qualified_name == "café")
    collect = next(item for item in definitions if item.qualified_name == "collect")
    callback = next(item for item in definitions if item.name.startswith("<anonymous@"))
    assert cafe.kind == "arrow_function"
    assert source[cafe.byte_start : cafe.byte_end] == b"async value => value"
    assert cafe.char_start == len("const café = ")
    assert collect.export_span is not None
    assert source[collect.export_span.byte_start : collect.export_span.byte_end] == (
        b"export const collect = () => [1].map(item => item);"
    )
    assert callback.kind == "arrow_function"
    assert callback.parent_native_ref == collect.native_ref
    assert callback.scope == ("collect",)
    assert source[callback.byte_start : callback.byte_end] == b"item => item"


def test_import_aliases_exports_inheritance_and_calls_keep_exact_scope_context():
    source = (
        b'import Client, { Helper as Tool, type Shape } from "./pkg";\n'
        b'import * as utils from "./utils";\n'
        b"export { Tool as PublicTool };\n"
        b'export { remote as Remote } from "./remote";\n'
        b"class Service extends Client implements Shape {\n"
        b"  load() { return Tool.run(utils.now()); }\n"
        b"}\n"
    )
    adapter = TypeScriptAdapter("typescript")

    syntax = adapter.parse(source, {})
    references = adapter.references(syntax)
    definitions = adapter.definitions(syntax)

    imports = [item for item in references if item.form == "import"]
    assert [
        (item.spelling, item.import_module, item.import_name, item.local_name)
        for item in imports
    ] == [
        ("Client", "./pkg", "default", "Client"),
        ("Helper as Tool", "./pkg", "Helper", "Tool"),
        ("type Shape", "./pkg", "Shape", "Shape"),
        ("* as utils", "./utils", "*", "utils"),
    ]
    exports = [item for item in references if item.form == "export"]
    assert [
        (item.spelling, item.import_module, item.import_name, item.local_name)
        for item in exports
    ] == [
        ("Tool as PublicTool", None, "Tool", "PublicTool"),
        ("remote as Remote", "./remote", "remote", "Remote"),
    ]
    assert [item.spelling for item in references if item.form == "inheritance"] == [
        "Client",
        "Shape",
    ]
    assert [item.spelling for item in references if item.form == "call"] == [
        "Tool.run",
        "utils.now",
    ]
    service = next(item for item in definitions if item.qualified_name == "Service")
    load = next(item for item in definitions if item.qualified_name == "Service.load")
    assert all(
        item.source_scope_ref == service.native_ref
        for item in references
        if item.form == "inheritance"
    )
    assert all(
        item.source_scope_ref == load.native_ref
        for item in references
        if item.form == "call"
    )
    assert [item.reason for item in adapter.context_spans(load)] == [
        "parent_declaration",
        "referenced_import",
        "referenced_import",
    ]


def test_malformed_declaration_is_diagnostic_while_valid_siblings_survive():
    source = b"function good() {}\nfunction broken(,) {}\nfunction later() {}\n"
    adapter = TypeScriptAdapter("typescript")

    syntax = adapter.parse(source, {})

    assert syntax.coverage == "partial"
    assert [item.name for item in adapter.definitions(syntax)] == ["good", "later"]
    assert [item.code for item in syntax.diagnostics] == ["typescript_syntax_error"]
    assert adapter.references(syntax) == ()
    assert [
        source[item.byte_start : item.byte_end] for item in syntax.uncovered_spans
    ] == [b"function broken(,) {}"]


def test_bom_unicode_and_line_endings_keep_original_coordinate_maps():
    source = (
        b"\xef\xbb\xbf"
        + "function café() { return 1; }\r\n".encode()
        + b"const value = 2;\r"
    )
    adapter = TypeScriptAdapter("typescript")

    syntax = adapter.parse(source, {})
    definition = adapter.definitions(syntax)[0]

    assert syntax.coverage == "parsed"
    assert syntax.encoding == "utf-8-sig"
    assert syntax.byte_offset(0) == 3
    assert syntax.char_offset(3) == 0
    assert definition.name == "café"
    assert definition.byte_start == 3
    assert definition.char_start == 0
    assert definition.line_start == 1
    assert syntax.line_starts == (0, 35, 52)
    assert (
        source[definition.byte_start : definition.byte_end].decode()
        == (syntax.text[definition.char_start : definition.char_end])
    )


def test_uncovered_statements_and_comments_keep_exact_source_without_false_names():
    source = (
        b"// Ghost() and class Phantom are comments\n"
        b"const status = readStatus();\n"
        b"export function live() { return status; }\n"
    )
    adapter = TypeScriptAdapter("typescript")

    syntax = adapter.parse(source, {})

    assert [item.name for item in adapter.definitions(syntax)] == ["live"]
    assert [
        item.spelling for item in adapter.references(syntax) if item.form == "call"
    ] == ["readStatus"]
    assert [
        source[item.byte_start : item.byte_end] for item in syntax.uncovered_spans
    ] == [
        b"// Ghost() and class Phantom are comments",
        b"const status = readStatus();",
    ]


def test_invalid_utf8_uses_a_labelled_exact_byte_fallback():
    source = b"const label = '\xff';\r"
    adapter = TypeScriptAdapter("typescript")

    syntax = adapter.parse(source, {})

    assert syntax.coverage == "text_fallback"
    assert syntax.encoding == "latin-1"
    assert syntax.text.encode("latin-1") == source
    assert syntax.byte_offset(len(syntax.text)) == len(source)
    assert syntax.line_starts == (0, len(source))
    assert [item.code for item in syntax.diagnostics] == ["invalid_source_encoding"]
    assert adapter.definitions(syntax) == ()
    assert adapter.references(syntax) == ()
