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
    assert (
        repository_manifest(files, commit=None, dirty=True)["files"][0]["relative_path"]
        == "a"
    )
    with pytest.raises(ValueError):
        repository_manifest(files + [files[0]], commit=None, dirty=True)
    assert new_family_nonce() != new_family_nonce()


@pytest.mark.parametrize(
    "value", [float("inf"), float("-inf"), {1: "bad"}, {"x": object()}]
)
def test_canonical_json_rejects_non_json(value):
    with pytest.raises((TypeError, ValueError)):
        canonical_bytes(value)
