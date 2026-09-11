from tychora.application.leases import LeaseSet


def test_nested_leases_keep_resources_pinned():
    leases = LeaseSet()
    with leases.pin(frozenset({"snapshot-a"})):
        with leases.pin(frozenset({"snapshot-a"})):
            assert leases.is_pinned("snapshot-a")
        assert leases.is_pinned("snapshot-a")
    assert not leases.is_pinned("snapshot-a")
