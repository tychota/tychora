"""TypeScript, TSX, JavaScript and JSX source extraction."""

from __future__ import annotations

from importlib.metadata import version
from typing import Literal

from tychora.contracts.identity import JsonValue, digest
from tychora.ports.language import (
    ContextReason,
    ContextSpan,
    Definition,
    LanguageCapabilities,
    Reference,
    SourceSpan,
    SyntaxDiagnostic,
    SyntaxResult,
)

from .syntax import base_syntax_result, make_span

Dialect = Literal["typescript", "tsx", "javascript", "jsx"]

_DIALECTS: tuple[Dialect, ...] = ("typescript", "tsx", "javascript", "jsx")
_GRAMMAR_PACKAGES = {
    "typescript": "tree-sitter-typescript",
    "tsx": "tree-sitter-typescript",
    "javascript": "tree-sitter-javascript",
    "jsx": "tree-sitter-javascript",
}
_EXTENSIONS = {
    "typescript": (".ts",),
    "tsx": (".tsx",),
    "javascript": (".js", ".mjs", ".cjs"),
    "jsx": (".jsx",),
}
_EXTRACTION_VERSION = "typescript-extraction-v1"
_DECLARATION_NODES = (
    "class_declaration",
    "interface_declaration",
    "function_declaration",
    "function_signature",
    "method_definition",
    "method_signature",
)


class TypeScriptAdapter:
    def __init__(self, dialect: Dialect) -> None:
        if dialect not in _DIALECTS:
            raise ValueError(f"Unsupported TypeScript dialect: {dialect}")
        self._dialect = dialect

    def capabilities(self) -> LanguageCapabilities:
        package = _GRAMMAR_PACKAGES[self._dialect]
        grammar_version = version(package)
        declaration_forms = (
            (
                "class",
                "interface",
                "function",
                "function_signature",
                "method",
                "method_signature",
                "arrow_function",
            )
            if self._dialect in ("typescript", "tsx")
            else ("class", "function", "method", "arrow_function")
        )
        fingerprint = digest(
            "language-adapter-v1",
            self._dialect,
            package,
            grammar_version,
            _EXTRACTION_VERSION,
        )
        return LanguageCapabilities(
            language=self._dialect,
            dialects=(self._dialect,),
            extensions=_EXTENSIONS[self._dialect],
            grammar_package=package,
            grammar_version=grammar_version,
            declaration_forms=declaration_forms,
            reference_forms=("import", "export", "call", "inheritance", "jsx"),
            limitations=(
                "dynamic calls are retained as unsupported references",
                "computed property definitions and their bodies are excluded from static scope extraction",
                "JSX element names are syntactic candidates rather than resolved runtime bindings",
                "decorators are reported only when the selected grammar exposes structural decorator nodes",
                "grammar-recovered syntax is diagnostic and excluded from structural references",
            ),
            adapter_fingerprint=fingerprint,
        )

    def parse(self, file_bytes: bytes, options: JsonValue) -> SyntaxResult:
        encoding = "utf-8-sig" if file_bytes.startswith(b"\xef\xbb\xbf") else "utf-8"
        fingerprint = digest(
            "language-parse-v1", self.capabilities().adapter_fingerprint, options
        )
        try:
            text = file_bytes.decode(encoding)
        except UnicodeError as error:
            text = file_bytes.decode("latin-1")
            fallback = base_syntax_result(
                language=self._dialect,
                grammar=self._grammar_name(),
                adapter_fingerprint=fingerprint,
                file_bytes=file_bytes,
                text=text,
                encoding="latin-1",
                coverage="text_fallback",
                options=options,
                root_type="text_fallback",
            )
            diagnostic = SyntaxDiagnostic(
                code="invalid_source_encoding",
                message=(
                    f"{self._dialect} source is not valid UTF-8; retained bytes "
                    f"exactly: {error}"
                ),
                severity="error",
                byte_start=0,
                byte_end=len(file_bytes),
                line_start=1,
                line_end=fallback.line_for_byte(max(0, len(file_bytes) - 1)),
            )
            return fallback.model_copy(update={"diagnostics": (diagnostic,)})
        parser = self._parser()
        tree = parser.parse(file_bytes)
        partial = tree.root_node.has_error
        syntax = base_syntax_result(
            language=self._dialect,
            grammar=self._grammar_name(),
            adapter_fingerprint=fingerprint,
            file_bytes=file_bytes,
            text=text,
            encoding=encoding,
            coverage="empty" if not file_bytes else "partial" if partial else "parsed",
            options=options,
            root_type=tree.root_node.type,
        )
        if partial:
            syntax = syntax.model_copy(
                update={
                    "diagnostics": tuple(
                        self._syntax_diagnostics(tree.root_node, syntax)
                    )
                }
            )
        definitions = tuple(self._definition_nodes(tree.root_node, syntax, ()))
        definitions_by_start = {
            definition.declaration_span.byte_start: definition
            for definition in definitions
        }
        references = tuple(
            self._reference_nodes(
                tree.root_node, syntax, (), None, definitions_by_start
            )
        )
        uncovered = tuple(self._uncovered_spans(tree.root_node, syntax))
        definitions = tuple(
            definition.model_copy(
                update={"context": self._contexts(definition, definitions, references)}
            )
            for definition in definitions
        )
        return syntax.model_copy(
            update={
                "definitions_found": definitions,
                "references_found": references,
                "uncovered_spans": uncovered,
            }
        )

    def definitions(self, syntax: SyntaxResult) -> tuple[Definition, ...]:
        self._require_dialect(syntax)
        return syntax.definitions_found

    def references(self, syntax: SyntaxResult) -> tuple[Reference, ...]:
        self._require_dialect(syntax)
        return syntax.references_found

    def context_spans(self, definition: Definition) -> tuple[ContextSpan, ...]:
        return definition.context

    def _parser(self):
        import tree_sitter_javascript
        import tree_sitter_typescript
        from tree_sitter import Language, Parser

        grammars = {
            "typescript": tree_sitter_typescript.language_typescript,
            "tsx": tree_sitter_typescript.language_tsx,
            "javascript": tree_sitter_javascript.language,
            "jsx": tree_sitter_javascript.language,
        }
        return Parser(Language(grammars[self._dialect]()))

    def _grammar_name(self) -> str:
        package = _GRAMMAR_PACKAGES[self._dialect]
        return f"{package}:{self._dialect}@{version(package)}"

    def _require_dialect(self, syntax: SyntaxResult) -> None:
        if syntax.language != self._dialect:
            raise ValueError(
                f"TypeScriptAdapter({self._dialect!r}) requires matching syntax facts"
            )

    def _definition_nodes(self, node, syntax: SyntaxResult, parents):
        pending_decorators = []
        for child in node.named_children:
            if child.type == "decorator":
                pending_decorators.append(child)
                continue
            wrapper = child if child.type == "export_statement" else None
            definition_node = (
                child.child_by_field_name("declaration")
                if wrapper is not None
                else child
            )
            if definition_node is None:
                pending_decorators.clear()
                continue
            if wrapper is not None:
                decorators = [
                    item for item in wrapper.named_children if item.type == "decorator"
                ]
            else:
                decorators = pending_decorators
            pending_decorators = []
            if definition_node.type in ("lexical_declaration", "variable_declaration"):
                yield from self._variable_definitions(
                    definition_node, wrapper, syntax, parents
                )
                continue
            if definition_node.type in ("arrow_function", "function_expression"):
                definition = self._function_expression_definition(
                    definition_node, None, None, syntax, parents
                )
                yield definition
                body = definition_node.child_by_field_name("body")
                if body is not None:
                    yield from self._definition_nodes(
                        body, syntax, (*parents, definition)
                    )
                continue
            if definition_node.type in _DECLARATION_NODES:
                if not self._valid_declaration(definition_node):
                    continue
                definition = self._definition(
                    definition_node, wrapper, decorators, syntax, parents
                )
                if definition is None:
                    continue
                yield definition
                body = definition_node.child_by_field_name("body")
                if body is not None:
                    yield from self._definition_nodes(
                        body, syntax, (*parents, definition)
                    )
                continue
            yield from self._definition_nodes(definition_node, syntax, parents)

    @staticmethod
    def _valid_declaration(node) -> bool:
        body = node.child_by_field_name("body")
        if body is None:
            return not node.has_error
        for child in node.children:
            if child == body:
                return True
            if child.has_error or child.is_error or child.is_missing:
                return False
        return False

    def _syntax_diagnostics(self, node, syntax):
        if node.is_error or node.is_missing:
            start = node.start_byte
            end = node.end_byte
            yield SyntaxDiagnostic(
                code=f"{self._dialect}_syntax_error",
                message=f"The {self._dialect} grammar recovered at {node.type}",
                severity="error",
                byte_start=start,
                byte_end=end,
                line_start=syntax.line_for_byte(start),
                line_end=syntax.line_for_byte(max(start, end - 1)),
            )
            return
        for child in node.children:
            if child.has_error or child.is_error or child.is_missing:
                yield from self._syntax_diagnostics(child, syntax)

    def _uncovered_spans(self, root, syntax):
        for node in root.named_children:
            if self._top_level_is_definition(node):
                continue
            if node.end_byte <= node.start_byte:
                continue
            yield self._span(
                syntax,
                f"{self._dialect}:uncovered:{node.start_byte}:{node.end_byte}",
                node.type,
                node.start_byte,
                node.end_byte,
            )

    @staticmethod
    def _top_level_is_definition(node) -> bool:
        declaration = (
            node.child_by_field_name("declaration")
            if node.type == "export_statement"
            else node
        )
        if declaration is None:
            return False
        if declaration.type in (
            "class_declaration",
            "interface_declaration",
            "function_declaration",
            "function_signature",
        ):
            return True
        if declaration.type not in ("lexical_declaration", "variable_declaration"):
            return False
        declarators = [
            child
            for child in declaration.named_children
            if child.type == "variable_declarator"
        ]
        return bool(declarators) and all(
            (value := item.child_by_field_name("value")) is not None
            and value.type in ("arrow_function", "function_expression")
            for item in declarators
        )

    def _variable_definitions(self, declaration, wrapper, syntax, parents):
        for declarator in declaration.named_children:
            if declarator.type != "variable_declarator":
                continue
            value = declarator.child_by_field_name("value")
            if value is None:
                continue
            if value.type not in ("arrow_function", "function_expression"):
                yield from self._definition_nodes(value, syntax, parents)
                continue
            binding = declarator.child_by_field_name("name")
            definition = self._function_expression_definition(
                value, binding, wrapper, syntax, parents
            )
            yield definition
            body = value.child_by_field_name("body")
            if body is not None:
                yield from self._definition_nodes(body, syntax, (*parents, definition))

    def _function_expression_definition(
        self, node, binding, wrapper, syntax: SyntaxResult, parents
    ) -> Definition:
        explicit_name = node.child_by_field_name("name")
        name_node = binding or explicit_name
        name = (
            self._text(name_node, syntax)
            if name_node is not None
            else f"<anonymous@{node.start_byte}>"
        )
        scope = tuple(parent.name for parent in parents)
        parent = parents[-1] if parents else None
        kind = "arrow_function" if node.type == "arrow_function" else "function"
        native_ref = f"{self._dialect}:{kind}:{node.start_byte}:{node.end_byte}"
        body = node.child_by_field_name("body")
        header_end = (
            node.end_byte
            if body is None
            else node.start_byte
            + len(syntax.file_bytes[node.start_byte : body.start_byte].rstrip())
        )
        byte_start = wrapper.start_byte if wrapper is not None else node.start_byte
        byte_end = wrapper.end_byte if wrapper is not None else node.end_byte
        return Definition(
            kind=kind,
            name=name,
            qualified_name=".".join((*scope, name)),
            scope=scope,
            native_ref=native_ref,
            byte_start=byte_start,
            byte_end=byte_end,
            char_start=syntax.char_offset(byte_start),
            char_end=syntax.char_offset(byte_end),
            line_start=syntax.line_for_byte(byte_start),
            line_end=syntax.line_for_byte(byte_end - 1),
            export_span=(
                self._span(
                    syntax,
                    native_ref,
                    "export",
                    wrapper.start_byte,
                    wrapper.end_byte,
                )
                if wrapper is not None
                else None
            ),
            declaration_span=self._span(
                syntax, native_ref, "declaration", node.start_byte, header_end
            ),
            parent_name=parent.qualified_name if parent is not None else None,
            parent_native_ref=parent.native_ref if parent is not None else None,
            overload_discriminator=node.start_byte,
        )

    def _reference_nodes(
        self,
        node,
        syntax: SyntaxResult,
        scope: tuple[str, ...],
        scope_ref: str | None,
        definitions_by_start: dict[int, Definition],
    ):
        for child in node.named_children:
            if child.type == "ERROR" or child.is_missing:
                continue
            if child.type == "import_statement":
                yield from self._import_references(child, syntax, scope, scope_ref)
                continue
            if child.type == "export_statement":
                yield from self._export_references(child, syntax, scope, scope_ref)
                yield from self._reference_nodes(
                    child, syntax, scope, scope_ref, definitions_by_start
                )
                continue
            definition = definitions_by_start.get(child.start_byte)
            if definition is not None:
                definition_scope = (*scope, definition.name)
                if child.type in ("class_declaration", "interface_declaration"):
                    yield from self._inheritance_references(
                        child,
                        syntax,
                        definition_scope,
                        definition.native_ref,
                    )
                name_node = child.child_by_field_name("name")
                for nested in child.named_children:
                    if nested == name_node or nested.type in (
                        "class_heritage",
                        "extends_type_clause",
                    ):
                        continue
                    yield from self._reference_nodes(
                        nested,
                        syntax,
                        definition_scope,
                        definition.native_ref,
                        definitions_by_start,
                    )
                continue
            if child.type in _DECLARATION_NODES:
                continue
            if child.has_error:
                continue
            if child.type == "call_expression":
                yield from self._call_references(
                    child, syntax, scope, scope_ref, definitions_by_start
                )
                continue
            if child.type in ("jsx_opening_element", "jsx_self_closing_element"):
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    yield self._expression_reference(
                        name_node, syntax, scope, scope_ref, "jsx"
                    )
                yield from self._reference_nodes(
                    child, syntax, scope, scope_ref, definitions_by_start
                )
                continue
            yield from self._reference_nodes(
                child, syntax, scope, scope_ref, definitions_by_start
            )

    def _import_references(self, statement, syntax, scope, scope_ref):
        source_node = statement.child_by_field_name("source")
        if source_node is None:
            return
        module = self._module_text(source_node, syntax)
        statement_span = self._span(
            syntax,
            f"{self._dialect}:import_statement:{statement.start_byte}:{statement.end_byte}",
            "import_statement",
            statement.start_byte,
            statement.end_byte,
        )
        clause = next(
            (item for item in statement.named_children if item.type == "import_clause"),
            None,
        )
        if clause is None:
            return
        for binding in clause.named_children:
            if binding.type == "identifier":
                name = self._text(binding, syntax)
                yield self._reference(
                    binding,
                    syntax,
                    scope,
                    scope_ref,
                    form="import",
                    spelling=name,
                    import_module=module,
                    import_name="default",
                    local_name=name,
                    candidate_names=(name,),
                    statement_span=statement_span,
                )
            elif binding.type == "named_imports":
                for specifier in binding.named_children:
                    name_node = specifier.child_by_field_name("name")
                    alias_node = specifier.child_by_field_name("alias")
                    if name_node is None:
                        continue
                    imported = self._text(name_node, syntax)
                    local = (
                        self._text(alias_node, syntax)
                        if alias_node is not None
                        else imported
                    )
                    yield self._reference(
                        specifier,
                        syntax,
                        scope,
                        scope_ref,
                        form="import",
                        spelling=self._text(specifier, syntax),
                        import_module=module,
                        import_name=imported,
                        local_name=local,
                        candidate_names=(f"{module}.{imported}", imported),
                        statement_span=statement_span,
                    )
            elif binding.type == "namespace_import" and binding.named_children:
                alias = binding.named_children[-1]
                local = self._text(alias, syntax)
                yield self._reference(
                    binding,
                    syntax,
                    scope,
                    scope_ref,
                    form="import",
                    spelling=self._text(binding, syntax),
                    import_module=module,
                    import_name="*",
                    local_name=local,
                    candidate_names=(module,),
                    statement_span=statement_span,
                )

    def _export_references(self, statement, syntax, scope, scope_ref):
        clause = next(
            (item for item in statement.named_children if item.type == "export_clause"),
            None,
        )
        if clause is None:
            return
        source_node = statement.child_by_field_name("source")
        module = self._module_text(source_node, syntax) if source_node else None
        statement_span = self._span(
            syntax,
            f"{self._dialect}:export_statement:{statement.start_byte}:{statement.end_byte}",
            "export_statement",
            statement.start_byte,
            statement.end_byte,
        )
        for specifier in clause.named_children:
            name_node = specifier.child_by_field_name("name")
            alias_node = specifier.child_by_field_name("alias")
            if name_node is None:
                continue
            imported = self._text(name_node, syntax)
            local = self._text(alias_node, syntax) if alias_node else imported
            yield self._reference(
                specifier,
                syntax,
                scope,
                scope_ref,
                form="export",
                spelling=self._text(specifier, syntax),
                import_module=module,
                import_name=imported,
                local_name=local,
                candidate_names=(imported,),
                statement_span=statement_span,
            )

    def _inheritance_references(self, declaration, syntax, scope, scope_ref):
        for child in declaration.named_children:
            if child.type == "class_heritage":
                saw_clause = False
                for clause in child.named_children:
                    if clause.type == "extends_clause":
                        saw_clause = True
                        value = clause.child_by_field_name("value")
                        if value is not None:
                            yield self._expression_reference(
                                value, syntax, scope, scope_ref, "inheritance"
                            )
                    elif clause.type == "implements_clause":
                        saw_clause = True
                        for value in clause.named_children:
                            yield self._expression_reference(
                                value, syntax, scope, scope_ref, "inheritance"
                            )
                if not saw_clause:
                    for value in child.named_children:
                        yield self._expression_reference(
                            value, syntax, scope, scope_ref, "inheritance"
                        )
            elif child.type == "extends_type_clause":
                for value in child.named_children:
                    yield self._expression_reference(
                        value, syntax, scope, scope_ref, "inheritance"
                    )

    def _call_references(self, call, syntax, scope, scope_ref, definitions_by_start):
        if call.has_error:
            return
        function = call.child_by_field_name("function")
        if function is not None:
            yield self._expression_reference(function, syntax, scope, scope_ref, "call")
            if function.type == "call_expression":
                yield from self._call_references(
                    function, syntax, scope, scope_ref, definitions_by_start
                )
            else:
                yield from self._reference_nodes(
                    function, syntax, scope, scope_ref, definitions_by_start
                )
        arguments = call.child_by_field_name("arguments")
        if arguments is not None:
            yield from self._reference_nodes(
                arguments, syntax, scope, scope_ref, definitions_by_start
            )

    def _expression_reference(self, node, syntax, scope, scope_ref, form):
        spelling = self._text(node, syntax)
        static_name = self._static_name(node, syntax)
        candidates = (
            tuple(dict.fromkeys((static_name, static_name.rsplit(".", 1)[-1])))
            if static_name is not None
            else ()
        )
        return self._reference(
            node,
            syntax,
            scope,
            scope_ref,
            form=form,
            spelling=spelling,
            candidate_names=candidates,
            supported=static_name is not None,
        )

    def _reference(
        self,
        node,
        syntax,
        scope,
        scope_ref,
        *,
        form,
        spelling,
        import_module=None,
        import_name=None,
        local_name=None,
        candidate_names=(),
        supported=True,
        statement_span=None,
    ):
        return Reference(
            form=form,
            spelling=spelling,
            native_ref=(
                f"{self._dialect}:reference:{form}:{node.start_byte}:{node.end_byte}"
            ),
            source_scope=scope,
            source_scope_ref=scope_ref,
            byte_start=node.start_byte,
            byte_end=node.end_byte,
            char_start=syntax.char_offset(node.start_byte),
            char_end=syntax.char_offset(node.end_byte),
            line_start=syntax.line_for_byte(node.start_byte),
            line_end=syntax.line_for_byte(node.end_byte - 1),
            import_module=import_module,
            import_name=import_name,
            local_name=local_name,
            candidate_names=candidate_names,
            supported=supported,
            statement_span=statement_span,
        )

    def _static_name(self, node, syntax) -> str | None:
        if node.type in (
            "identifier",
            "type_identifier",
            "property_identifier",
            "private_property_identifier",
            "nested_identifier",
        ):
            return self._text(node, syntax)
        if node.type not in ("member_expression", "subscript_expression"):
            return None
        owner = node.child_by_field_name("object")
        property_node = node.child_by_field_name("property")
        if (
            owner is None
            or property_node is None
            or node.type == "subscript_expression"
        ):
            return None
        owner_name = self._static_name(owner, syntax)
        if owner_name is None or property_node.type not in (
            "property_identifier",
            "private_property_identifier",
        ):
            return None
        return f"{owner_name}.{self._text(property_node, syntax)}"

    @staticmethod
    def _module_text(node, syntax) -> str:
        text = syntax.file_bytes[node.start_byte : node.end_byte].decode("utf-8")
        return text[1:-1] if len(text) >= 2 and text[0] in "'\"" else text

    def _definition(
        self, node, wrapper, decorators, syntax: SyntaxResult, parents
    ) -> Definition | None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or name_node.type == "computed_property_name":
            return None
        name = self._text(name_node, syntax)
        scope = tuple(parent.name for parent in parents)
        parent = parents[-1] if parents else None
        kinds = {
            "class_declaration": "class",
            "interface_declaration": "interface",
            "function_declaration": "function",
            "function_signature": "function_signature",
            "method_definition": "method",
            "method_signature": "method_signature",
        }
        kind = kinds[node.type]
        native_ref = f"{self._dialect}:{kind}:{node.start_byte}:{node.end_byte}"
        header_end = (
            node.end_byte
            if body is None
            else node.start_byte
            + len(syntax.file_bytes[node.start_byte : body.start_byte].rstrip())
        )
        byte_start = (
            wrapper.start_byte
            if wrapper is not None
            else decorators[0].start_byte
            if decorators
            else node.start_byte
        )
        return Definition(
            kind=kind,
            name=name,
            qualified_name=".".join((*scope, name)),
            scope=scope,
            native_ref=native_ref,
            byte_start=byte_start,
            byte_end=node.end_byte,
            char_start=syntax.char_offset(byte_start),
            char_end=syntax.char_offset(node.end_byte),
            line_start=syntax.line_for_byte(byte_start),
            line_end=syntax.line_for_byte(node.end_byte - 1),
            decorator_span=(
                self._span(
                    syntax,
                    native_ref,
                    "decorators",
                    decorators[0].start_byte,
                    decorators[-1].end_byte,
                )
                if decorators
                else None
            ),
            export_span=(
                self._span(
                    syntax,
                    native_ref,
                    "export",
                    wrapper.start_byte,
                    wrapper.end_byte,
                )
                if wrapper is not None
                else None
            ),
            declaration_span=self._span(
                syntax, native_ref, "declaration", node.start_byte, header_end
            ),
            parent_name=parent.qualified_name if parent is not None else None,
            parent_native_ref=parent.native_ref if parent is not None else None,
            overload_discriminator=node.start_byte,
        )

    def _contexts(
        self,
        definition: Definition,
        definitions: tuple[Definition, ...],
        references: tuple[Reference, ...],
    ) -> tuple[ContextSpan, ...]:
        contexts: list[ContextSpan] = []
        definitions_by_ref = {item.native_ref: item for item in definitions}
        if definition.parent_native_ref is not None:
            parent = definitions_by_ref[definition.parent_native_ref]
            contexts.append(
                self._context(
                    parent.declaration_span,
                    parent.native_ref,
                    "parent_declaration",
                )
            )

        used_names = {
            item.candidate_names[0].split(".", 1)[0]
            for item in references
            if item.form not in ("import", "export")
            and item.supported
            and item.candidate_names
            and item.source_scope_ref == definition.native_ref
        }
        visible_scope_refs: set[str | None] = {None, definition.native_ref}
        parent_ref = definition.parent_native_ref
        while parent_ref is not None:
            visible_scope_refs.add(parent_ref)
            parent_ref = definitions_by_ref[parent_ref].parent_native_ref
        selected = []
        definition_scope = (*definition.scope, definition.name)
        for name in sorted(used_names):
            visible = [
                item
                for item in references
                if item.form == "import"
                and item.local_name == name
                and item.source_scope_ref in visible_scope_refs
                and definition_scope[: len(item.source_scope)] == item.source_scope
            ]
            if visible:
                selected.append(max(visible, key=lambda item: len(item.source_scope)))
        seen: set[tuple[int, int]] = set()
        for imported in sorted(selected, key=lambda item: item.byte_start):
            statement = imported.statement_span
            if statement is None:
                continue
            key = (statement.byte_start, statement.byte_end)
            if key in seen:
                continue
            seen.add(key)
            contexts.append(
                self._context(statement, statement.native_ref, "referenced_import")
            )
        return tuple(contexts)

    @staticmethod
    def _context(
        span: SourceSpan, source_native_ref: str, reason: ContextReason
    ) -> ContextSpan:
        return ContextSpan(
            source_native_ref=source_native_ref,
            byte_start=span.byte_start,
            byte_end=span.byte_end,
            char_start=span.char_start,
            char_end=span.char_end,
            line_start=span.line_start,
            line_end=span.line_end,
            reason=reason,
            dependency_native_refs=(source_native_ref,),
        )

    @staticmethod
    def _text(node, syntax: SyntaxResult) -> str:
        return syntax.file_bytes[node.start_byte : node.end_byte].decode("utf-8")

    @staticmethod
    def _span(
        syntax: SyntaxResult,
        native_ref: str,
        kind: str,
        byte_start: int,
        byte_end: int,
    ) -> SourceSpan:
        return make_span(
            syntax,
            native_ref=f"{native_ref}:{kind}",
            kind=kind,
            byte_start=byte_start,
            byte_end=byte_end,
        )
