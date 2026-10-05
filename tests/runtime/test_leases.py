from tychora.application.leases import LeaseSet


def test_nested_leases_keep_resources_pinned():
    leases = LeaseSet()
    with leases.pin(frozenset({"snapshot-a"})):
        with leases.pin(frozenset({"snapshot-a"})):
            assert leases.is_pinned("snapshot-a")
        assert leases.is_pinned("snapshot-a")
    assert not leases.is_pinned("snapshot-a")


def test_destructive_lease_fence_rejects_existing_and_blocks_new_pins():
    from threading import Event, Thread

    leases = LeaseSet()
    with leases.pin(frozenset({"old"})), leases.exclusive_if_idle() as idle:
        assert not idle

    attempted, acquired = Event(), Event()

    def reader():
        attempted.set()
        with leases.pin(frozenset({"new"})):
            acquired.set()

    with leases.exclusive_if_idle() as idle:
        assert idle
        thread = Thread(target=reader)
        thread.start()
        assert attempted.wait(1)
        assert not acquired.wait(0.05)
    assert acquired.wait(1)
    thread.join(timeout=1)
    assert not thread.is_alive()
