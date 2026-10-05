"""Code assertion v2 changes identity while retained v1 proof still replays."""

import json

from tests.code.test_structure import _registration
from tychora.adapters.code import normalize as code_normalize
from tychora.configuration.registry import Registry
from tychora.configuration.resolve import resolve_config


def test_new_code_assertions_have_distinct_identity_and_legacy_receipt_replays(
    tmp_path, monkeypatch
):
    settings = tmp_path / "settings.toml"
    settings.write_text('schema_version=1\n[profiles.test]\nbuiltin="code@1"\n')
    registry = Registry.from_config(resolve_config(settings, "test", {}))
    artifacts, registration, parsed = _registration(tmp_path)
    original = code_normalize._fingerprints

    def old_fingerprints(source, files, selected_registry):
        parser, _ = original(source, files, selected_registry)
        return (
            parser,
            code_normalize._legacy_normalizer_fingerprint(source, selected_registry),
        )

    with monkeypatch.context() as temporary:
        temporary.setattr(code_normalize, "_fingerprints", old_fingerprints)
        old_retained = code_normalize.retain_code_extraction(
            registration, parsed, registry, artifacts
        )
    old_replayed = code_normalize.replay_code_extraction(
        old_retained.raw_document_ref, registration, artifacts
    )
    old_bundle = code_normalize.normalize_code(old_replayed, registry)
    new_retained = code_normalize.retain_code_extraction(
        registration, parsed, registry, artifacts
    )
    new_replayed = code_normalize.replay_code_extraction(
        new_retained.raw_document_ref, registration, artifacts
    )
    new_bundle = code_normalize.normalize_code(new_replayed, registry)
    assert old_retained.normalizer_fingerprint != new_retained.normalizer_fingerprint
    assert old_bundle.extraction.extraction_id != new_bundle.extraction.extraction_id
    assert old_retained.assertions_ref != new_retained.assertions_ref
    with artifacts.open_verified(old_retained.assertions_ref) as stream:
        old_assertions = [json.loads(line)["data"] for line in stream]
    with artifacts.open_verified(new_retained.assertions_ref) as stream:
        new_assertions = [json.loads(line)["data"] for line in stream]
    assert any(item["subject"]["kind"] == "symbol" for item in old_assertions)
    assert all(item["subject"]["kind"] == "code_file@1" for item in new_assertions)
    assert len(new_assertions) == len(old_assertions)
