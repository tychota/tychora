from tychora.adapters.code.normalize import range_dependencies
from tychora.contracts.code import OwnedRange


def owned(owner: str, start: int, end: int, *, path: str = "src/main.py"):
    return OwnedRange(
        owner_unit_id=owner,
        artifact_ref="a" * 64,
        relative_path=path,
        char_start=start,
        char_end=end,
        byte_start=start,
        byte_end=end,
    )


def test_dependencies_include_every_overlapping_owner_at_boundaries():
    dependencies = range_dependencies(
        (
            owned("parent", 0, 20),
            owned("accepted-child", 2, 8),
            owned("rejected-child", 8, 15),
            owned("after", 20, 24),
        )
    )
    assert dependencies["parent"] == frozenset(
        {"parent", "accepted-child", "rejected-child"}
    )
    assert dependencies["accepted-child"] == frozenset({"parent", "accepted-child"})
    assert dependencies["rejected-child"] == frozenset({"parent", "rejected-child"})
    assert dependencies["after"] == frozenset({"after"})


def test_equal_ranges_in_different_files_do_not_share_owners():
    dependencies = range_dependencies(
        (owned("a", 0, 4, path="a.py"), owned("b", 0, 4, path="b.py"))
    )
    assert dependencies == {"a": frozenset({"a"}), "b": frozenset({"b"})}
