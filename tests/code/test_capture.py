import json

import pytest

from tychora.adapters.code.capture import parse_cache_key


def entries(captured):
    return {entry.relative_path: entry for entry in captured.entries}


def test_dirty_bytes_are_not_attributed_only_to_head(repo, capture):
    first = capture(repo)
    (repo / "module.py").write_text("def revised(): return 2\n")
    second = capture(repo)
    assert first.original_sha != second.original_sha
    assert second.dirty is True
    assert first.commit == second.commit


def test_noop_capture_is_stable_and_one_change_has_one_parse_input(repo, capture):
    first = capture(repo)
    repeated = capture(repo)
    assert first.original_sha == repeated.original_sha
    assert first.dirty is False
    assert repeated.dirty is False

    (repo / "module.py").write_text("def changed(): return 3\n")
    changed = capture(repo)
    before, after = entries(first), entries(changed)
    changed_paths = [
        path for path in after if before.get(path).blob_sha != after[path].blob_sha
    ]
    parse_inputs = {
        parse_cache_key(after[path].blob_sha, "python@1", {"dialect": "py312"})
        for path in changed_paths
    }
    assert len(changed.entries) == 2
    assert changed_paths == ["module.py"]
    assert len(parse_inputs) == 1


def test_inventory_tracks_untracked_deleted_renamed_and_unusual_paths(repo, capture):
    from tests.code.conftest import git

    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "ignored.txt").write_text("ignored\n")
    (repo / "new file\nline.py").write_text("value = 1\n")
    (repo / "module.py").unlink()
    git(repo, "mv", "notes.txt", "renamed notes.txt")

    result = capture(repo)
    assert tuple(entries(result)) == (
        ".gitignore",
        "new file\nline.py",
        "renamed notes.txt",
    )
    assert result.dirty is True


def test_dirty_scope_ignores_excluded_only_changes(repo, capture):
    from tests.code.conftest import git

    private = repo / "private"
    private.mkdir()
    (private / "generated.py").write_text("token = 'synthetic'\n")
    git(repo, "add", "private/generated.py")
    git(repo, "commit", "-qm", "add excluded fixture")
    baseline = capture(repo, excludes=("private/**",))
    (private / "generated.py").write_text("token = 'changed'\n")
    changed = capture(repo, excludes=("private/**",))
    assert changed.original_sha == baseline.original_sha
    assert changed.dirty is False
    assert "private/generated.py" not in entries(changed)


@pytest.mark.parametrize("pattern", ("[^a].txt", "[[:digit:]].txt"))
def test_repository_rejects_bracket_patterns_with_different_git_semantics(
    repo, capture, pattern
):
    with pytest.raises(ValueError, match="repository exclude"):
        capture(repo, excludes=(pattern,))


def test_supported_bracket_range_excludes_same_head_and_dirty_paths(repo, capture):
    from tests.code.conftest import git

    (repo / "a.txt").write_text("a")
    (repo / "b.txt").write_text("b")
    (repo / "c.txt").write_text("c")
    git(repo, "add", "a.txt", "b.txt", "c.txt")
    git(repo, "commit", "-qm", "add synthetic brackets")
    baseline = capture(repo, excludes=("[a-b].txt",))
    (repo / "a.txt").write_text("changed")
    repeated = capture(repo, excludes=("[a-b].txt",))
    assert tuple(entries(baseline)) == tuple(entries(repeated))
    assert "a.txt" not in entries(repeated)
    assert "b.txt" not in entries(repeated)
    assert "c.txt" in entries(repeated)
    assert repeated.dirty is False


def test_exclude_star_is_segment_bounded_while_double_star_crosses_directories(
    repo, capture
):
    from tests.code.conftest import git

    private = repo / "private"
    nested = private / "nested"
    nested.mkdir(parents=True)
    (private / "direct.py").write_text("direct = True\n")
    (nested / "deep.py").write_text("deep = True\n")
    git(repo, "add", "private/direct.py", "private/nested/deep.py")
    git(repo, "commit", "-qm", "add exclude fixtures")

    direct_only = capture(repo, excludes=("private/*",))
    assert "private/direct.py" not in entries(direct_only)
    assert "private/nested/deep.py" in entries(direct_only)
    assert direct_only.dirty is False

    whole_subtree = capture(repo, excludes=("private/**",))
    assert "private/direct.py" not in entries(whole_subtree)
    assert "private/nested/deep.py" not in entries(whole_subtree)
    assert whole_subtree.dirty is False


def test_staged_and_unstaged_included_changes_are_dirty(repo, capture):
    from tests.code.conftest import git

    (repo / "module.py").write_text("unstaged = True\n")
    assert capture(repo).dirty is True
    git(repo, "add", "module.py")
    assert capture(repo).dirty is True


def test_detached_head_and_executable_mode_are_explicit(repo, capture):
    from tests.code.conftest import git

    git(repo, "checkout", "--detach", "-q")
    detached = capture(repo)
    assert detached.commit is not None
    assert detached.dirty is False
    (repo / "module.py").chmod(0o755)
    changed = capture(repo)
    assert entries(changed)["module.py"].mode == 0o755
    assert changed.dirty is True


def test_unmerged_index_captures_worktree_bytes_as_dirty(repo, capture):
    import subprocess

    from tests.code.conftest import git

    base_branch = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "branch", "--show-current"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    git(repo, "checkout", "-qb", "topic")
    (repo / "module.py").write_text("topic = True\n")
    git(repo, "commit", "-qam", "topic change")
    git(repo, "checkout", "-q", base_branch)
    (repo / "module.py").write_text("main = True\n")
    git(repo, "commit", "-qam", "main change")
    merged = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "merge", "topic"],
        check=False,
        capture_output=True,
    )
    assert merged.returncode != 0
    result = capture(repo)
    assert result.dirty is True
    with capture.artifacts.open_verified(
        entries(result)["module.py"].artifact_ref
    ) as stream:
        assert b"<<<<<<<" in stream.read()


def test_original_is_typed_sorted_repository_manifest(repo, capture):
    result = capture(repo)
    with capture.artifacts.open_verified(result.original_ref) as stream:
        original = json.load(stream)
    assert original["type"] == "repository_capture"
    assert original["data"]["dirty_scope"] == "captured_paths_vs_head"
    assert [item["relative_path"] for item in original["data"]["files"]] == [
        "module.py",
        "notes.txt",
    ]
    assert all(
        item["blob_sha"] == item["artifact_ref"]["sha256"]
        for item in original["data"]["files"]
    )


def test_parse_cache_key_binds_bytes_grammar_and_canonical_options():
    blob = "a" * 64
    first = parse_cache_key(blob, "python@1", {"b": 2, "a": 1})
    assert first == parse_cache_key(blob, "python@1", {"a": 1, "b": 2})
    assert first != parse_cache_key("b" * 64, "python@1", {"a": 1, "b": 2})
    assert first != parse_cache_key(blob, "python@2", {"a": 1, "b": 2})
    assert first != parse_cache_key(blob, "python@1", {"a": 2, "b": 2})
