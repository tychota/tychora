"""Persistent syntax reuse validates identity and never deserializes code."""

import json
from hashlib import sha256

import pytest

from tychora.adapters.code.parse_cache import SyntaxCache, file_adapter, parse_identity
from tychora.contracts.base import FrozenJsonMap


def test_reopened_cache_rebinds_path_and_invalidates_grammar_options(tmp_path):
    payload = b"def hello(): return 1\n"
    options = FrozenJsonMap({})
    identity = parse_identity("original.py", options)
    syntax = file_adapter("original.py").parse(payload, {})
    cache = SyntaxCache(tmp_path / "cache")
    cache.put(syntax, identity, options)
    reopened = SyntaxCache(tmp_path / "cache")
    assert (
        reopened.get(payload, parse_identity("renamed.pyi", options), options) == syntax
    )
    assert (
        reopened.get(payload, parse_identity("original.ts", options), options) is None
    )
    assert (
        reopened.get(payload, (identity[0], "grammar@changed", identity[2]), options)
        is None
    )
    changed = FrozenJsonMap({"future_parser_option": True})
    assert (
        reopened.get(payload, parse_identity("original.py", changed), changed) is None
    )
    assert reopened.get(payload + b"# new bytes\n", identity, options) is None
    path = (
        tmp_path / "cache" / cache.key(sha256(payload).hexdigest(), identity, options)
    )
    data = json.loads(path.read_bytes())
    assert "file_bytes" not in data["syntax"]
    assert "original.py" not in path.read_text()
    path.write_bytes(b"corrupt")
    assert reopened.get(payload, identity, options) is None
    cache.put(syntax, identity, options)
    assert reopened.get(payload, identity, options) == syntax


@pytest.mark.parametrize("unsafe", ["symlink", "directory", "tamper"])
def test_unsafe_or_tampered_cache_entry_is_a_miss(tmp_path, unsafe):
    payload = b"print('ok')\n"
    options = FrozenJsonMap({})
    identity = parse_identity("x.py", options)
    syntax = file_adapter("x.py").parse(payload, {})
    cache = SyntaxCache(tmp_path / "cache")
    cache.put(syntax, identity, options)
    path = (
        tmp_path / "cache" / cache.key(sha256(payload).hexdigest(), identity, options)
    )
    saved = path.read_bytes()
    path.unlink()
    if unsafe == "symlink":
        external = tmp_path / "external"
        external.write_bytes(saved)
        path.symlink_to(external)
    elif unsafe == "directory":
        path.mkdir()
    else:
        data = json.loads(saved)
        data["syntax"]["text"] = "wrong text"
        path.write_text(json.dumps(data))
    assert cache.get(payload, identity, options) is None


def test_repository_cache_persists_across_reopen_and_restore_needs_no_cache(
    tmp_path, monkeypatch
):
    from tests.api.test_read_grants import Counter
    from tests.code.conftest import make_repo
    from tychora import Tychora
    from tychora.application.owner import WorkspaceOwner
    from tychora.configuration.resolve import resolve_config

    repo = make_repo(tmp_path)
    settings = tmp_path / "settings.toml"
    settings.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.extraction]\nparser="code"\n'
        f'[configuration.acquisition]\nroots=["{tmp_path}"]\n'
    )
    config = resolve_config(settings, "test", {})
    source = {
        "type": "repository",
        "path": str(repo),
        "source_key": "code",
        "collection_id": "default",
        "kind": "code_file@1",
        "metadata": {"title": "Synthetic repository"},
    }
    original = WorkspaceOwner.run_parser
    calls = []

    async def counted(self, parser, artifacts, ctx):
        calls.append(True)
        return await original(self, parser, artifacts, ctx)

    monkeypatch.setattr(WorkspaceOwner, "run_parser", counted)
    workspace = tmp_path / "kb"
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        assert kb.jobs.wait(kb.sources.add(source).id).state == "succeeded"
        assert len(calls) == 2
        old = kb.search({"question": "original", "modalities": ["lexical"]}).segments[0]
    (repo / "module.py").write_text("def changed(): return 2\n")
    calls.clear()
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        assert kb.jobs.wait(kb.sources.add(source).id).state == "succeeded"
        assert len(calls) == 1
        assert kb.evidence.get(old.matched_unit_ids[0]).evidence is not None
        current = kb.search(
            {"question": "changed", "modalities": ["lexical"]}
        ).segments[0]
        assert current.revision_id != old.revision_id
        assert not set(current.matched_unit_ids) & set(old.matched_unit_ids)
    settings.write_text(
        settings.read_text().replace(
            'parser="code"', 'parser="code"\nrepresentation_version="2"'
        )
    )
    config = resolve_config(settings, "test", {})
    calls.clear()
    with Tychora.open(workspace, config=config, token_counter=Counter()) as kb:
        assert kb.jobs.wait(kb.sources.add(source).id).state == "succeeded"
        assert len(calls) == 2
        reparsed = kb.search(
            {"question": "changed", "modalities": ["lexical"]}
        ).segments[0]
        assert reparsed.revision_id == current.revision_id
        assert reparsed.extraction_id != current.extraction_id
        assert kb.evidence.get(current.matched_unit_ids[0]).evidence is not None
        calls.clear()
        assert kb.jobs.wait(kb.sources.add(source).id).state == "succeeded"
        assert calls == []
        archive = tmp_path / "backup"
        kb.management.run({"operation": "export", "destination": str(archive)})
    restored = tmp_path / "restored"
    with Tychora.restore(archive, restored) as kb:
        assert not (restored / "cache" / "syntax-v1").exists()
        assert kb.evidence.get(current.matched_unit_ids[0]).evidence is not None
        assert kb.evidence.get(old.matched_unit_ids[0]).evidence is not None
    (repo / "module.py").write_text("def restored_edit(): return 3\n")
    calls.clear()
    with Tychora.open(restored, config=config, token_counter=Counter()) as kb:
        assert kb.jobs.wait(kb.sources.add(source).id).state == "succeeded"
        assert len(calls) == 2
