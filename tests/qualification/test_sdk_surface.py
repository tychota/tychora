"""Offline SDK surface qualification for optional capabilities."""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.qualification


@pytest.mark.asyncio
async def test_mcp_server_exposes_streamable_http_and_session_lifecycle():
    from mcp.server import MCPServer

    server = MCPServer("qualification-probe")
    app = server.streamable_http_app()

    assert app is not None
    assert callable(server.session_manager.run)
    async with server.session_manager.run():
        assert server.session_manager is not None


def test_voyage_contextual_embedding_and_explicit_tokenizer_surfaces():
    import voyageai

    contextual_parameters = inspect.signature(
        voyageai.Client.contextualized_embed
    ).parameters
    tokenizer_parameters = inspect.signature(voyageai.Client.tokenizer).parameters
    tokenize_parameters = inspect.signature(voyageai.Client.tokenize).parameters

    assert "enable_auto_chunking" in contextual_parameters
    assert contextual_parameters["enable_auto_chunking"].default is False
    assert list(tokenizer_parameters) == ["self", "model"]
    assert list(tokenize_parameters) == ["self", "texts", "model"]


def test_docling_document_preserves_json_and_embedded_image():
    from docling_core.types.doc import DoclingDocument, ImageRef
    from PIL import Image

    source_image = Image.new("RGB", (3, 2), color=(12, 34, 56))
    document = DoclingDocument(name="synthetic-image")
    picture = document.add_picture(image=ImageRef.from_pil(source_image, dpi=72))

    exported = document.export_to_dict()
    recovered_image = picture.get_image(document)

    assert exported["name"] == "synthetic-image"
    assert exported["pictures"][0]["image"]["uri"].startswith("data:image/png;base64,")
    assert recovered_image is not None
    assert recovered_image.size == (3, 2)
    assert recovered_image.getpixel((0, 0)) == (12, 34, 56)


@pytest.mark.parametrize(
    ("module_name", "factory_name", "source", "root_type"),
    [
        ("tree_sitter_python", "language", b"value = 1\n", "module"),
        ("tree_sitter_javascript", "language", b"const value = 1;\n", "program"),
        (
            "tree_sitter_typescript",
            "language_typescript",
            b"const value: number = 1;\n",
            "program",
        ),
        (
            "tree_sitter_typescript",
            "language_tsx",
            b"const view = <main />;\n",
            "program",
        ),
    ],
)
def test_tree_sitter_grammar_wheel_parses_supported_language(
    module_name: str, factory_name: str, source: bytes, root_type: str
):
    import importlib

    from tree_sitter import Language, Parser

    grammar_module = importlib.import_module(module_name)
    grammar: object = getattr(grammar_module, factory_name)()
    language = Language(grammar)
    tree = Parser(language).parse(source)

    assert tree.root_node.type == root_type
    assert not tree.root_node.has_error
