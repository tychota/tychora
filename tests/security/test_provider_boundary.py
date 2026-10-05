"""Offline operation excludes external provider construction and dispatch."""

from tychora import Tychora
from tychora.configuration.resolve import resolve_config


class _Counter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, value):
        return len(value)


def test_offline_profile_never_constructs_external_transport(tmp_path, monkeypatch):
    settings = tmp_path / "workspace.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        "[configuration]\noffline=true\n"
        '[configuration.providers.text]\nprovider="voyage"\n'
        'model="voyage-context-4"\ncredential_ref="env:UNSET_TEST_KEY"\n'
        '[configuration.index.text]\nbackend="disabled"\n'
    )
    config = resolve_config(settings, "test", {})

    def prohibited(*args, **kwargs):
        raise AssertionError("offline composition constructed an external client")

    monkeypatch.setattr(
        "tychora.application.default_bindings.HttpxVoyageTransport", prohibited
    )
    monkeypatch.setattr("tychora.application.default_bindings._runtime", prohibited)
    with Tychora.open(
        tmp_path / "workspace", config=config, token_counter=_Counter()
    ) as kb:
        bindings = kb._workspace.services.provider_bindings
        assert bindings.text_index is None
        assert bindings.text_query is None
        assert bindings.text_embedder is None
        handle = kb.sources.add(
            {
                "type": "text",
                "source_key": "offline",
                "collection_id": "default",
                "kind": "manual@1",
                "format": "text",
                "text": "A locally searchable offline fixture.",
                "metadata": {
                    "title": "Offline fixture",
                    "publisher": "Synthetic fixture",
                    "edition": "1",
                },
            }
        )
        assert kb.jobs.wait(handle.id).state == "succeeded"
        assert kb.search({"question": "offline", "modalities": ["lexical"]}).segments
