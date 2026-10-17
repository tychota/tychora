import math

import pytest

from tychora.contracts.identity import canonical_bytes, digest


def test_identity_is_order_stable_and_preserves_source_bytes():
    assert canonical_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert digest("revision-v1", "é") != digest("revision-v1", "e\u0301")
    with pytest.raises(ValueError):
        canonical_bytes({"score": math.nan})


@pytest.mark.parametrize(
    "key", ["/abs/file", "../file", "a/../b", "C:/file", "a\\b", "", "."]
)
def test_source_key_rejects_non_relative_or_ambiguous_keys(key):
    from uuid import UUID

    from tychora.contracts.identity import source_id

    with pytest.raises(ValueError):
        source_id(UUID(int=1), key)


def test_source_identity_is_lexical_and_preserves_case():
    from uuid import UUID, uuid5

    from tychora.contracts.identity import source_id

    collection = UUID(int=1)
    assert source_id(collection, "./docs//file.md") == str(
        uuid5(collection, "docs/file.md")
    )
    assert source_id(collection, "docs/File.md") != source_id(
        collection, "docs/file.md"
    )


def test_six_identity_formulas_are_domain_separated():
    from tychora.contracts import identity as ids

    assert ids.revision_id("s", "o", "e", "m") == digest(
        "revision-v1", "s", "o", "e", "m"
    )
    assert ids.extraction_id("r", "p", "n") == digest("extraction-v1", "r", "p", "n")
    assert ids.unit_id("e", "ref", 0, "t") == digest("unit-v1", "e", "ref", 0, "t")
    assert ids.chunk_id("u", 0, 4, "c") == digest("chunk-v1", "u", 0, 4, "c")
    assert ids.space_id("text", "p", "m", 2, "cosine", "r") == digest(
        "space-v1", "text", "p", "m", 2, "cosine", "r"
    )
    assert ids.vector_record_id("f", "c", "i") == digest("vector-v1", "f", "c", "i")
    assert ids.revision_id("s", "o", "e2", "m") != ids.revision_id("s", "o", "e", "m")


def test_repository_manifest_order_and_family_nonce():
    from tychora.contracts.identity import new_family_nonce, repository_manifest

    files = [("z", "100644", "sha-z", "ref-z"), ("a", "100755", "sha-a", "ref-a")]
    assert repository_manifest(files, commit=None, dirty=True) == repository_manifest(
        files[::-1], commit=None, dirty=True
    )
    manifest_files = repository_manifest(files, commit=None, dirty=True)["files"]
    assert isinstance(manifest_files, list)
    first_file = manifest_files[0]
    assert isinstance(first_file, dict)
    assert first_file["relative_path"] == "a"
    with pytest.raises(ValueError):
        repository_manifest(files + [files[0]], commit=None, dirty=True)
    assert new_family_nonce() != new_family_nonce()


@pytest.mark.parametrize(
    "value", [float("inf"), float("-inf"), {1: "bad"}, {"x": object()}]
)
def test_canonical_json_rejects_non_json(value):
    with pytest.raises((TypeError, ValueError)):
        canonical_bytes(value)


def test_canonical_bytes_are_compact_sorted_utf8():
    assert (
        canonical_bytes({"é": [1, 2.5, True, None]})
        == '{"é":[1,2.5,true,null]}'.encode()
    )
    assert canonical_bytes(None) == b"null"
    assert canonical_bytes(("a",)) == b'["a"]'
    assert canonical_bytes([]) == b"[]"


def test_non_json_values_fail_with_their_rule():
    with pytest.raises(ValueError, match=r"^JSON object keys must be strings$"):
        canonical_bytes({1: "bad"})  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match=r"^Only JSON values are supported$"):
        canonical_bytes({"x": object()})  # ty: ignore[invalid-argument-type]


def test_source_key_errors_name_the_rule():
    from tychora.contracts.identity import normalize_source_key

    for key in ("a\x00b", "a\\b", "/abs", "C:/file", "a/../b", ""):
        with pytest.raises(
            ValueError,
            match=r"^Source key must be a relative POSIX path without parent traversal$",
        ):
            normalize_source_key(key)
    with pytest.raises(ValueError, match=r"^Source key must name a source$"):
        normalize_source_key("./.")
    assert normalize_source_key("a//b/./c") == "a/b/c"


def test_identity_formulas_are_pinned_to_their_published_digests():
    from uuid import UUID

    from tychora.contracts import identity as ids

    assert digest("revision-v1", "s", "o", "e", "m") == (
        "61fe8bc2c51051183c620769103182359a3f6825e71d4a3be6f01e77f21e2431"
    )
    assert ids.family_id("w", "c", "n") == digest("family-v1", "w", "c", "n")
    assert ids.family_id("w", "c", "n") == (
        "597768bb3e4d8482a4cb6872a364198735f0449c6a5f9150c59f72750d216127"
    )
    assert ids.vector_record_id("f", "c", "i") == (
        "f9ead3014f9bb291caaf04c98c5719e645ed9d74e6e0a02a46e10d893a078fbb"
    )
    assert (
        ids.source_id(UUID(int=1), "docs/file.md")
        == "ace3e175-b406-55e1-accd-45a027c107ae"
    )


def test_repository_manifest_has_exact_shape():
    from tychora.contracts.identity import repository_manifest

    files = [("b", "100644", "sha-b", "ref-b"), ("./a", "100755", "sha-a", "ref-a")]
    assert repository_manifest(files, commit="abc", dirty=False) == {
        "files": [
            {
                "relative_path": "a",
                "mode": "100755",
                "blob_sha": "sha-a",
                "artifact_ref": "ref-a",
            },
            {
                "relative_path": "b",
                "mode": "100644",
                "blob_sha": "sha-b",
                "artifact_ref": "ref-b",
            },
        ],
        "commit": "abc",
        "dirty": False,
    }
    with pytest.raises(ValueError, match=r"^Duplicate repository path$"):
        repository_manifest(
            [*files, ("a", "100644", "x", "y")], commit=None, dirty=True
        )
