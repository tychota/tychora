import pytest

from tychora.adapters.code.python import PythonAdapter


def test_python_decorated_async_method_ranges():
    data = b"class Store:\n    @logged\n    async def load(self):\n        return 1\n"
    adapter = PythonAdapter()
    defs = adapter.definitions(adapter.parse(data, {}))
    method = next(d for d in defs if d.qualified_name == "Store.load")
    assert data[method.byte_start : method.byte_end].startswith(b"@logged")
    assert method.parent_name == "Store"
    assert method.decorator_span is not None
    assert (
        data[method.decorator_span.byte_start : method.decorator_span.byte_end]
        == b"@logged"
    )
    assert (
        data[method.declaration_span.byte_start : method.declaration_span.byte_end]
        == b"async def load(self):"
    )


def test_nested_classes_repeated_names_and_docstrings_are_distinct():
    data = b'''class Outer:
    """Container docs."""
    class Inner:
        def run(self):
            """First run."""
            return 1

        def run(self):
            return 2
'''
    adapter = PythonAdapter()
    definitions = adapter.definitions(adapter.parse(data, {}))

    inner = next(item for item in definitions if item.qualified_name == "Outer.Inner")
    runs = [item for item in definitions if item.qualified_name == "Outer.Inner.run"]
    assert inner.parent_name == "Outer"
    assert len(runs) == 2
    assert len({item.native_ref for item in runs}) == 2
    assert runs[0].parent_name == "Outer.Inner"
    assert runs[0].docstring_span is not None
    doc = runs[0].docstring_span
    assert data[doc.byte_start : doc.byte_end] == b'"""First run."""'


def test_repeated_definitions_do_not_share_local_import_context():
    data = (
        b"def load():\n"
        b"    import os\n"
        b"    return os.getcwd()\n"
        b"def load():\n"
        b"    return 2\n"
    )
    adapter = PythonAdapter()
    syntax = adapter.parse(data, {})
    loads = [item for item in adapter.definitions(syntax) if item.name == "load"]

    assert len(loads) == 2
    assert [
        [span.reason for span in adapter.context_spans(definition)]
        for definition in loads
    ] == [["referenced_import"], []]
    call = next(item for item in adapter.references(syntax) if item.form == "call")
    assert call.source_scope_ref == loads[0].native_ref


def test_import_call_inheritance_references_supply_exact_definition_context():
    data = (
        b"from pkg.client import Client as ApiClient, Other\n"
        b"import helpers.tools as tools, os\n"
        b"class Service(ApiClient, Other):\n"
        b"    def load(self):\n"
        b"        return tools.fetch(os.getcwd())\n"
    )
    adapter = PythonAdapter()
    syntax = adapter.parse(data, {})
    references = adapter.references(syntax)

    imports = [item for item in references if item.form == "import"]
    assert [
        (item.spelling, item.import_module, item.import_name, item.local_name)
        for item in imports
    ] == [
        ("Client as ApiClient", "pkg.client", "Client", "ApiClient"),
        ("Other", "pkg.client", "Other", "Other"),
        ("helpers.tools as tools", "helpers.tools", None, "tools"),
        ("os", "os", None, "os"),
    ]
    assert [item.spelling for item in references if item.form == "inheritance"] == [
        "ApiClient",
        "Other",
    ]
    assert [item.spelling for item in references if item.form == "call"] == [
        "tools.fetch",
        "os.getcwd",
    ]

    definitions = adapter.definitions(syntax)
    service = next(item for item in definitions if item.qualified_name == "Service")
    load = next(item for item in definitions if item.qualified_name == "Service.load")
    service_context = adapter.context_spans(service)
    assert [item.reason for item in service_context] == ["referenced_import"]
    assert (
        data[service_context[0].byte_start : service_context[0].byte_end]
        == b"from pkg.client import Client as ApiClient, Other"
    )
    load_context = adapter.context_spans(load)
    assert [item.reason for item in load_context] == [
        "parent_declaration",
        "referenced_import",
    ]
    assert data[load_context[0].byte_start : load_context[0].byte_end] == (
        b"class Service(ApiClient, Other):"
    )
    assert data[load_context[1].byte_start : load_context[1].byte_end] == (
        b"import helpers.tools as tools, os"
    )


def test_calls_in_decorators_defaults_and_bodies_keep_definition_scope():
    data = (
        b"from helpers import logged, build\n"
        b"@logged(build())\n"
        b"def load(value=build()):\n"
        b"    return build()\n"
    )
    adapter = PythonAdapter()
    syntax = adapter.parse(data, {})
    calls = [item for item in adapter.references(syntax) if item.form == "call"]

    assert [(item.spelling, item.source_scope) for item in calls] == [
        ("logged", ("load",)),
        ("build", ("load",)),
        ("build", ("load",)),
        ("build", ("load",)),
    ]
    definition = adapter.definitions(syntax)[0]
    context = adapter.context_spans(definition)
    assert [item.reason for item in context] == ["referenced_import"]
    assert data[context[0].byte_start : context[0].byte_end] == (
        b"from helpers import logged, build"
    )


def test_chained_calls_keep_nested_static_callees_without_duplicates():
    data = (
        b"def chained():\n"
        b"    return factory()()\n"
        b"def attributed():\n"
        b"    return factory().value()\n"
    )
    adapter = PythonAdapter()
    calls = [
        item
        for item in adapter.references(adapter.parse(data, {}))
        if item.form == "call"
    ]

    assert [(item.spelling, item.supported, item.source_scope) for item in calls] == [
        ("factory()", False, ("chained",)),
        ("factory", True, ("chained",)),
        ("factory().value", False, ("attributed",)),
        ("factory", True, ("attributed",)),
    ]


def test_syntax_recovery_is_diagnostic_and_does_not_invent_calls():
    data = b"def broken(:\n    missing(\n"
    adapter = PythonAdapter()
    syntax = adapter.parse(data, {})

    assert syntax.coverage == "partial"
    assert syntax.text == data.decode("utf-8")
    assert any(item.code == "python_syntax_error" for item in syntax.diagnostics)
    assert [item for item in adapter.references(syntax) if item.form == "call"] == []


def test_syntax_recovery_keeps_valid_surrounding_definitions():
    data = (
        b"class Store:\n"
        b"    def good(self):\n"
        b"        return 1\n"
        b"    def broken(self):\n"
        b"        missing(\n"
        b"    def later(self):\n"
        b"        return 2\n"
    )
    adapter = PythonAdapter()
    syntax = adapter.parse(data, {})

    assert syntax.coverage == "partial"
    assert [item.qualified_name for item in adapter.definitions(syntax)] == [
        "Store",
        "Store.good",
        "Store.later",
    ]
    assert [item for item in adapter.references(syntax) if item.form == "call"] == []


def test_uncovered_meaningful_module_statements_keep_exact_ranges():
    data = b"import os\nSTATUS = os.getenv('STATUS')\nprint(STATUS)\n"
    syntax = PythonAdapter().parse(data, {})

    assert [
        data[item.byte_start : item.byte_end] for item in syntax.uncovered_spans
    ] == [
        b"import os",
        b"STATUS = os.getenv('STATUS')",
        b"print(STATUS)",
    ]
    assert all(
        syntax.text[item.char_start : item.char_end]
        == data[item.byte_start : item.byte_end].decode("utf-8")
        for item in syntax.uncovered_spans
    )


def test_latin1_cookie_uses_labelled_faithful_text_fallback():
    data = b"# coding: latin-1\ndef caf\xe9():\n    return '\xe9'\n"
    adapter = PythonAdapter()
    syntax = adapter.parse(data, {})

    assert syntax.encoding == "iso-8859-1"
    assert syntax.coverage == "text_fallback"
    assert (
        syntax.text
        == "# coding: latin-1\ndef caf\N{LATIN SMALL LETTER E WITH ACUTE}():\n    return '\N{LATIN SMALL LETTER E WITH ACUTE}'\n"
    )
    assert syntax.byte_offset(len(syntax.text)) == len(data)
    assert all(
        syntax.char_offset(syntax.byte_offset(offset)) == offset
        for offset in range(len(syntax.text) + 1)
    )
    assert adapter.definitions(syntax) == ()
    assert [item.code for item in syntax.diagnostics] == ["unsupported_parser_encoding"]


def test_unknown_cookie_preserves_bytes_in_an_explicit_fallback_view():
    data = b"# coding: x-not-a-codec\nvalue = '\xff'\n"
    syntax = PythonAdapter().parse(data, {})

    assert syntax.coverage == "text_fallback"
    assert syntax.encoding == "latin-1"
    assert syntax.text.encode("latin-1") == data
    assert syntax.byte_offset(len(syntax.text)) == len(data)
    assert [item.code for item in syntax.diagnostics] == ["unsupported_source_encoding"]
    assert syntax.definitions_found == ()
    assert syntax.references_found == ()


def test_invalid_default_utf8_is_a_labelled_faithful_fallback():
    data = b"label = '\xff'\n"
    syntax = PythonAdapter().parse(data, {})

    assert syntax.coverage == "text_fallback"
    assert syntax.encoding == "latin-1"
    assert syntax.text.encode("latin-1") == data
    assert [item.code for item in syntax.diagnostics] == ["invalid_source_encoding"]


def test_invalid_utf8_after_encoding_probe_lines_is_a_faithful_fallback():
    data = b"# coding: utf-8\n\nlabel = '\xff'\n"
    syntax = PythonAdapter().parse(data, {})

    assert syntax.coverage == "text_fallback"
    assert syntax.encoding == "latin-1"
    assert syntax.text.encode("latin-1") == data
    assert [item.code for item in syntax.diagnostics] == ["invalid_source_encoding"]
    assert syntax.definitions_found == ()
    assert syntax.references_found == ()


def test_utf8_bom_and_multibyte_identifier_round_trip_original_offsets():
    body = "def caf\N{LATIN SMALL LETTER E WITH ACUTE}():\n    return '\N{LATIN SMALL LETTER E WITH ACUTE}'\n".encode()
    data = b"\xef\xbb\xbf" + body
    adapter = PythonAdapter()
    syntax = adapter.parse(data, {})
    definition = adapter.definitions(syntax)[0]

    assert syntax.encoding == "utf-8-sig"
    assert syntax.coverage == "parsed"
    assert syntax.byte_offset(0) == 3
    assert syntax.char_offset(3) == 0
    with pytest.raises(ValueError, match="inside an encoded codepoint"):
        syntax.char_offset(data.index(b"\xc3") + 1)
    assert (
        data[definition.byte_start : definition.byte_end].decode("utf-8")
        == (syntax.text[definition.char_start : definition.char_end])
    )
    assert definition.name == "caf\N{LATIN SMALL LETTER E WITH ACUTE}"
    assert definition.byte_start == 3
    assert definition.char_start == 0
    assert definition.line_start == 1
    assert definition.line_end == 2


def test_cr_only_source_has_exact_lines_and_explicit_parser_fallback():
    data = b"def one():\r    return 1\rdef two():\r    return 2\r"
    syntax = PythonAdapter().parse(data, {})

    assert syntax.coverage == "text_fallback"
    assert syntax.text.encode("utf-8") == data
    assert syntax.line_starts == (0, 11, 24, 35, 48)
    assert syntax.line_for_byte(24) == 3
    assert [item.code for item in syntax.diagnostics] == [
        "unsupported_parser_line_endings"
    ]
    assert syntax.definitions_found == ()

    crlf = PythonAdapter().parse(
        b"def one():\r\n    pass\r\ndef two():\r\n    pass\r\n", {}
    )
    assert crlf.coverage == "parsed"
    assert crlf.line_starts == (0, 12, 22, 34, 44)
    assert [item.line_start for item in crlf.definitions_found] == [1, 3]


def test_empty_file_is_explicit_immutable_coverage():
    syntax = PythonAdapter().parse(b"", {})

    assert syntax.coverage == "empty"
    assert syntax.text == ""
    assert syntax.codepoint_to_byte == (0,)
    assert syntax.line_starts == (0,)
    assert syntax.definitions_found == ()
    assert syntax.references_found == ()


def test_capabilities_and_parse_fingerprint_bind_grammar_and_options():
    adapter = PythonAdapter()
    capabilities = adapter.capabilities()
    first = adapter.parse(b"value = 1\n", {"target": "3.12", "strict": True})
    reordered = adapter.parse(b"value = 1\n", {"strict": True, "target": "3.12"})
    changed = adapter.parse(b"value = 1\n", {"target": "3.12", "strict": False})

    assert capabilities.language == "python"
    assert capabilities.grammar_package == "tree-sitter-python"
    assert capabilities.dialects == ("python3",)
    assert first.adapter_fingerprint == reordered.adapter_fingerprint
    assert first.adapter_fingerprint != changed.adapter_fingerprint
