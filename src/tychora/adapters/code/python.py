"""Python 3 source extraction using the installed Tree-sitter grammar."""

from __future__ import annotations

import io
import tokenize
from importlib.metadata import version

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

_GRAMMAR = "tree-sitter-python"
_EXTRACTION_VERSION = "python-extraction-v1"


class PythonAdapter:
    def capabilities(self) -> LanguageCapabilities:
        grammar_version = version(_GRAMMAR)
        fingerprint = digest(
            "language-adapter-v1",
            "python",
            _GRAMMAR,
            grammar_version,
            _EXTRACTION_VERSION,
        )
        return LanguageCapabilities(
            language="python",
            dialects=("python3",),
            extensions=(".py", ".pyi"),
            grammar_package=_GRAMMAR,
            grammar_version=grammar_version,
            declaration_forms=("class", "function", "async_function", "method"),
            reference_forms=("import", "call", "inheritance"),
            limitations=(
                "dynamic imports are not statically identified",
                "computed calls remain unresolved",
            ),
            adapter_fingerprint=fingerprint,
        )

    def parse(self, file_bytes: bytes, options: JsonValue) -> SyntaxResult:
        adapter_fingerprint = digest(
            "language-parse-v1", self.capabilities().adapter_fingerprint, options
        )
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(file_bytes).readline)
        except (LookupError, SyntaxError, UnicodeError) as error:
            text = file_bytes.decode("latin-1")
            fallback = base_syntax_result(
                language="python",
                grammar=self._grammar_name(),
                adapter_fingerprint=adapter_fingerprint,
                file_bytes=file_bytes,
                text=text,
                encoding="latin-1",
                coverage="text_fallback",
                options=options,
                root_type="text_fallback",
            )
            diagnostic = SyntaxDiagnostic(
                code=(
                    "unsupported_source_encoding"
                    if "unknown encoding" in str(error).lower()
                    else "invalid_source_encoding"
                ),
                message=f"Python encoding detection failed; retained bytes exactly: {error}",
                severity="error",
                byte_start=0,
                byte_end=len(file_bytes),
                line_start=1,
                line_end=fallback.line_for_byte(max(0, len(file_bytes) - 1)),
            )
            return fallback.model_copy(update={"diagnostics": (diagnostic,)})
        text = file_bytes.decode(encoding)
        if encoding.lower().replace("_", "-") not in ("utf-8", "utf-8-sig"):
            fallback = base_syntax_result(
                language="python",
                grammar=self._grammar_name(),
                adapter_fingerprint=adapter_fingerprint,
                file_bytes=file_bytes,
                text=text,
                encoding=encoding,
                coverage="text_fallback",
                options=options,
                root_type="text_fallback",
            )
            diagnostic = SyntaxDiagnostic(
                code="unsupported_parser_encoding",
                message=(
                    f"The installed Python grammar cannot safely parse {encoding} "
                    "bytes; retained an exact decoded text fallback"
                ),
                severity="warning",
                byte_start=0,
                byte_end=len(file_bytes),
                line_start=1,
                line_end=fallback.line_for_byte(max(0, len(file_bytes) - 1)),
            )
            return fallback.model_copy(update={"diagnostics": (diagnostic,)})

        parser = self._parser()
        tree = parser.parse(file_bytes)
        partial = tree.root_node.has_error
        base = base_syntax_result(
            language="python",
            grammar=self._grammar_name(),
            adapter_fingerprint=adapter_fingerprint,
            file_bytes=file_bytes,
            text=text,
            encoding=encoding,
            coverage="empty" if not file_bytes else "partial" if partial else "parsed",
            options=options,
            root_type=tree.root_node.type,
        )
        if partial:
            base = base.model_copy(
                update={"diagnostics": self._syntax_diagnostics(tree.root_node, base)}
            )
        definitions = tuple(self._definition_nodes(tree.root_node, base))
        references = tuple(self._reference_nodes(tree.root_node, base, ()))
        uncovered = tuple(self._uncovered_spans(tree.root_node, base))
        definitions = tuple(
            definition.model_copy(
                update={"context": self._contexts(definition, definitions, references)}
            )
            for definition in definitions
        )
        return base.model_copy(
            update={
                "definitions_found": definitions,
                "references_found": references,
                "uncovered_spans": uncovered,
            }
        )

    def definitions(self, syntax: SyntaxResult) -> tuple[Definition, ...]:
        self._require_python(syntax)
        return syntax.definitions_found

    def references(self, syntax: SyntaxResult) -> tuple[Reference, ...]:
        self._require_python(syntax)
        return syntax.references_found

    def context_spans(self, definition: Definition) -> tuple[ContextSpan, ...]:
        return definition.context

    @staticmethod
    def _parser():
        import tree_sitter_python
        from tree_sitter import Language, Parser

        return Parser(Language(tree_sitter_python.language()))

    @staticmethod
    def _grammar_name() -> str:
        return f"{_GRAMMAR}@{version(_GRAMMAR)}"

    @staticmethod
    def _require_python(syntax: SyntaxResult) -> None:
        if syntax.language != "python":
            raise ValueError("PythonAdapter requires Python syntax facts")

    def _definition_nodes(self, root, syntax: SyntaxResult):
        yield from self._walk_definitions(root, syntax, ())

    def _uncovered_spans(self, root, syntax: SyntaxResult):
        for node in root.named_children:
            definition_node = node
            if node.type == "decorated_definition":
                definition_node = node.child_by_field_name("definition")
            if definition_node is not None and definition_node.type in (
                "class_definition",
                "function_definition",
            ):
                continue
            if node.end_byte <= node.start_byte:
                continue
            native_ref = f"python:uncovered:{node.start_byte}:{node.end_byte}"
            yield self._span(
                syntax,
                native_ref,
                node.type,
                node.start_byte,
                node.end_byte,
            )

    def _reference_nodes(self, node, syntax: SyntaxResult, scope: tuple[str, ...]):
        for child in node.named_children:
            if child.type == "ERROR" or child.is_missing:
                continue
            definition_node = child
            if child.type == "decorated_definition":
                definition_node = child.child_by_field_name("definition")
            if definition_node is not None and definition_node.type in (
                "class_definition",
                "function_definition",
            ):
                body = definition_node.child_by_field_name("body")
                if body is None or not self._valid_header(definition_node, body):
                    continue
                name_node = definition_node.child_by_field_name("name")
                if name_node is None:
                    continue
                definition_scope = (*scope, self._text(name_node, syntax))
                if child.type == "decorated_definition":
                    for decorator in child.named_children:
                        if decorator.type == "decorator":
                            yield from self._reference_nodes(
                                decorator, syntax, definition_scope
                            )
                if definition_node.type == "class_definition":
                    superclasses = definition_node.child_by_field_name("superclasses")
                    if superclasses is not None:
                        for superclass in superclasses.named_children:
                            yield self._expression_reference(
                                superclass,
                                syntax,
                                definition_scope,
                                "inheritance",
                            )
                for header_child in definition_node.named_children:
                    if header_child not in (name_node, body):
                        yield from self._reference_nodes(
                            header_child, syntax, definition_scope
                        )
                yield from self._reference_nodes(body, syntax, definition_scope)
                continue
            if child.type in ("import_statement", "import_from_statement"):
                yield from self._import_references(child, syntax, scope)
                continue
            if child.type == "call":
                if not child.has_error:
                    function = child.child_by_field_name("function")
                    if function is not None:
                        yield self._expression_reference(
                            function, syntax, scope, "call"
                        )
                arguments = child.child_by_field_name("arguments")
                if arguments is not None:
                    yield from self._reference_nodes(arguments, syntax, scope)
                continue
            yield from self._reference_nodes(child, syntax, scope)

    def _walk_definitions(self, node, syntax: SyntaxResult, parents):
        for child in node.named_children:
            wrapper = None
            definition_node = child
            if child.type == "decorated_definition":
                wrapper = child
                definition_node = child.child_by_field_name("definition")
            if definition_node is not None and definition_node.type in (
                "class_definition",
                "function_definition",
            ):
                definition = self._definition(definition_node, wrapper, syntax, parents)
                if definition is None:
                    continue
                yield definition
                body = definition_node.child_by_field_name("body")
                if body is not None:
                    yield from self._walk_definitions(
                        body, syntax, (*parents, definition)
                    )
                continue
            yield from self._walk_definitions(child, syntax, parents)

    def _import_references(
        self, statement, syntax: SyntaxResult, scope: tuple[str, ...]
    ):
        module_node = (
            statement.child_by_field_name("module_name")
            if statement.type == "import_from_statement"
            else None
        )
        from_module = (
            self._text(module_node, syntax) if module_node is not None else None
        )
        statement_ref = (
            f"python:import_statement:{statement.start_byte}:{statement.end_byte}"
        )
        statement_span = self._span(
            syntax,
            statement_ref,
            "import_statement",
            statement.start_byte,
            statement.end_byte,
        )
        bindings = [
            child
            for index, child in enumerate(statement.children)
            if statement.field_name_for_child(index) == "name"
        ]
        for binding in bindings:
            name_node = (
                binding.child_by_field_name("name")
                if binding.type == "aliased_import"
                else binding
            )
            alias_node = (
                binding.child_by_field_name("alias")
                if binding.type == "aliased_import"
                else None
            )
            if name_node is None:
                continue
            imported = self._text(name_node, syntax)
            alias = self._text(alias_node, syntax) if alias_node is not None else None
            if from_module is None:
                import_module = imported
                import_name = None
                local_name = alias or imported.split(".", 1)[0]
                candidates = (imported,)
            else:
                import_module = from_module
                import_name = imported
                local_name = alias or imported.rsplit(".", 1)[-1]
                candidates = (f"{from_module}.{imported}", imported)
            yield self._reference(
                binding,
                syntax,
                scope,
                form="import",
                spelling=self._text(binding, syntax),
                import_module=import_module,
                import_name=import_name,
                local_name=local_name,
                candidate_names=tuple(dict.fromkeys(candidates)),
                supported=imported != "*",
                statement_span=statement_span,
            )

    def _expression_reference(
        self,
        node,
        syntax: SyntaxResult,
        scope: tuple[str, ...],
        form: str,
    ) -> Reference:
        spelling = self._text(node, syntax)
        static_name = self._static_name(node, syntax)
        candidates = ()
        if static_name is not None:
            candidates = tuple(
                dict.fromkeys((static_name, static_name.rsplit(".", 1)[-1]))
            )
        return self._reference(
            node,
            syntax,
            scope,
            form=form,
            spelling=spelling,
            candidate_names=candidates,
            supported=static_name is not None,
        )

    def _reference(
        self,
        node,
        syntax: SyntaxResult,
        scope: tuple[str, ...],
        *,
        form: str,
        spelling: str,
        import_module: str | None = None,
        import_name: str | None = None,
        local_name: str | None = None,
        candidate_names: tuple[str, ...] = (),
        supported: bool,
        statement_span: SourceSpan | None = None,
    ) -> Reference:
        return Reference(
            form=form,
            spelling=spelling,
            native_ref=f"python:reference:{form}:{node.start_byte}:{node.end_byte}",
            source_scope=scope,
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

    def _static_name(self, node, syntax: SyntaxResult) -> str | None:
        if node.type in ("identifier", "dotted_name"):
            return self._text(node, syntax)
        if node.type != "attribute":
            return None
        owner = node.child_by_field_name("object")
        attribute = node.child_by_field_name("attribute")
        if owner is None or attribute is None:
            return None
        owner_name = self._static_name(owner, syntax)
        if owner_name is None or attribute.type != "identifier":
            return None
        return f"{owner_name}.{self._text(attribute, syntax)}"

    @staticmethod
    def _text(node, syntax: SyntaxResult) -> str:
        return syntax.file_bytes[node.start_byte : node.end_byte].decode("utf-8")

    def _contexts(
        self,
        definition: Definition,
        definitions: tuple[Definition, ...],
        references: tuple[Reference, ...],
    ) -> tuple[ContextSpan, ...]:
        contexts: list[ContextSpan] = []
        if definition.parent_native_ref is not None:
            parent = next(
                item
                for item in definitions
                if item.native_ref == definition.parent_native_ref
            )
            contexts.append(
                self._context(
                    parent.declaration_span,
                    parent.native_ref,
                    "parent_declaration",
                )
            )

        definition_scope = (*definition.scope, definition.name)
        used_names = {
            item.spelling.split(".", 1)[0]
            for item in references
            if item.form != "import"
            and item.supported
            and item.source_scope == definition_scope
        }
        selected_imports: list[Reference] = []
        for name in sorted(used_names):
            visible = [
                item
                for item in references
                if item.form == "import"
                and item.local_name == name
                and definition_scope[: len(item.source_scope)] == item.source_scope
            ]
            if visible:
                selected_imports.append(
                    max(visible, key=lambda item: len(item.source_scope))
                )
        seen_statements: set[tuple[int, int]] = set()
        for imported in sorted(selected_imports, key=lambda item: item.byte_start):
            statement = imported.statement_span
            if statement is None:
                continue
            key = (statement.byte_start, statement.byte_end)
            if key in seen_statements:
                continue
            seen_statements.add(key)
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

    def _definition(
        self, node, wrapper, syntax: SyntaxResult, parents
    ) -> Definition | None:
        name_node = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name_node is None or body is None or not self._valid_header(node, body):
            return None
        name = syntax.file_bytes[name_node.start_byte : name_node.end_byte].decode(
            "utf-8"
        )
        scope = tuple(parent.name for parent in parents)
        qualified_name = ".".join((*scope, name))
        parent = parents[-1] if parents else None
        kind = (
            "class"
            if node.type == "class_definition"
            else "method"
            if parent is not None and parent.kind == "class"
            else "function"
        )
        byte_start = wrapper.start_byte if wrapper is not None else node.start_byte
        header_end = self._trim_header(
            syntax.file_bytes, node.start_byte, body.start_byte
        )
        native_ref = f"python:{kind}:{node.start_byte}:{node.end_byte}"
        declaration_span = self._span(
            syntax, native_ref, "declaration", node.start_byte, header_end
        )
        decorator_span = self._decorator_span(wrapper, node, syntax, native_ref)
        docstring_span = self._docstring_span(body, syntax, native_ref)
        return Definition(
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            scope=scope,
            native_ref=native_ref,
            byte_start=byte_start,
            byte_end=node.end_byte,
            char_start=syntax.char_offset(byte_start),
            char_end=syntax.char_offset(node.end_byte),
            line_start=syntax.line_for_byte(byte_start),
            line_end=syntax.line_for_byte(node.end_byte - 1),
            decorator_span=decorator_span,
            declaration_span=declaration_span,
            docstring_span=docstring_span,
            parent_name=parent.qualified_name if parent is not None else None,
            parent_native_ref=parent.native_ref if parent is not None else None,
            overload_discriminator=node.start_byte,
        )

    @staticmethod
    def _trim_header(data: bytes, start: int, end: int) -> int:
        return start + len(data[start:end].rstrip())

    @staticmethod
    def _valid_header(node, body) -> bool:
        for child in node.children:
            if child == body:
                return True
            if child.has_error or child.is_error or child.is_missing:
                return False
        return False

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

    def _decorator_span(
        self, wrapper, node, syntax: SyntaxResult, native_ref: str
    ) -> SourceSpan | None:
        if wrapper is None:
            return None
        decorators = [
            child for child in wrapper.named_children if child.type == "decorator"
        ]
        if not decorators:
            return None
        return self._span(
            syntax,
            native_ref,
            "decorators",
            decorators[0].start_byte,
            decorators[-1].end_byte,
        )

    def _docstring_span(
        self, body, syntax: SyntaxResult, native_ref: str
    ) -> SourceSpan | None:
        if not body.named_children:
            return None
        statement = body.named_children[0]
        if statement.type != "expression_statement" or not statement.named_children:
            return None
        value = statement.named_children[0]
        if value.type not in ("string", "concatenated_string"):
            return None
        return self._span(
            syntax,
            native_ref,
            "docstring",
            value.start_byte,
            value.end_byte,
        )

    def _syntax_diagnostics(
        self, root, syntax: SyntaxResult
    ) -> tuple[SyntaxDiagnostic, ...]:
        diagnostics: list[SyntaxDiagnostic] = []

        def visit(node) -> None:
            if node.is_error or node.is_missing:
                byte_start = node.start_byte
                byte_end = node.end_byte
                line_byte = byte_start if byte_end == byte_start else byte_end - 1
                diagnostics.append(
                    SyntaxDiagnostic(
                        code="python_syntax_error",
                        message=(
                            "Tree-sitter recovered a missing syntax node"
                            if node.is_missing
                            else "Tree-sitter recovered invalid Python syntax"
                        ),
                        severity="error",
                        byte_start=byte_start,
                        byte_end=byte_end,
                        line_start=syntax.line_for_byte(byte_start),
                        line_end=syntax.line_for_byte(line_byte),
                    )
                )
                if node.is_error:
                    return
            for child in node.named_children:
                visit(child)

        visit(root)
        return tuple(diagnostics)
