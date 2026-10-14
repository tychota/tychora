import multiprocessing
import os
import time
from threading import Timer

import pytest

from tests.acceptance.parser_crash_probe import blocked_parser
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


@pytest.mark.parametrize("cancel", [False, True])
def test_bounded_parser_reaps_before_return(tmp_path, cancel):
    from tychora.application.runtime import ParserExecutionLimits, parser_process

    path = tmp_path / "input"
    path.write_text(str(tmp_path))
    ctx = ExecutionContext()
    timer = Timer(0.1, ctx.cancelled.set) if cancel else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(TychoraError) as error:
            parser_process(
                blocked_parser,
                (path,),
                limits=ParserExecutionLimits(
                    page_count=1,
                    wall_seconds=5 if cancel else 0.1,
                    rss_bytes=4294967296,
                    output_bytes=268435456,
                    context=ctx,
                ),
            )
        assert error.value.code == ("CANCELLED" if cancel else "DEADLINE_EXCEEDED")
        assert time.monotonic() - started < 4
        marker = tmp_path / "parser-child-live"
        if marker.exists():
            with pytest.raises(ProcessLookupError):
                os.kill(int(marker.read_text()), 0)
        assert not multiprocessing.active_children()
    finally:
        if timer:
            timer.join()


class BatchParser:
    def run(self, paths, report):
        from tychora.application.owner import StagedOutput
        from tychora.application.runtime import ParserOutputBatch

        output = paths[0].with_name("batch.json")
        output.write_bytes(b"{}")
        output.chmod(0o600)
        report(
            ParserOutputBatch((StagedOutput(output.name, "application/json", 2),), (1,))
        )
        assert paths[0].with_name("ack").exists()
        output.unlink()
        return "finished"


def test_batch_is_acknowledged_only_after_collection(tmp_path):
    from tychora.application.runtime import ParserExecutionLimits, parser_process

    path = tmp_path / "input"
    path.write_bytes(b"x")
    accepted = []

    def collect(batch):
        assert (tmp_path / batch.outputs[0].name).read_bytes() == b"{}"
        accepted.extend(batch.pages)
        (tmp_path / "ack").touch()

    result = parser_process(
        BatchParser(),
        (path,),
        limits=ParserExecutionLimits(1, 10, 4294967296, 268435456, ExecutionContext()),
        progress=collect,
    )
    assert result == "finished"
    assert accepted == [1]
    assert not (tmp_path / "batch.json").exists()


def test_parent_collection_remains_deadline_bounded(tmp_path):
    from concurrent.futures import Future

    from tychora.application.runtime import ParserExecutionLimits, parser_process

    path = tmp_path / "input"
    path.write_bytes(b"x")
    future = Future()
    timer = Timer(3.5, lambda: future.set_result(None))
    timer.start()
    try:
        with pytest.raises(TychoraError) as error:
            parser_process(
                BatchParser(),
                (path,),
                limits=ParserExecutionLimits(1, 3, 4294967296, 100, ExecutionContext()),
                progress=lambda _: future,
            )
        assert error.value.code == "DEADLINE_EXCEEDED"
        assert (
            future.done()
        )  # Descriptor holder drained before caller may clean staging.
        assert not multiprocessing.active_children()
    finally:
        timer.join()


def test_output_overrun_is_rejected_before_collection(tmp_path):
    from tychora.application.runtime import ParserExecutionLimits, parser_process

    path = tmp_path / "input"
    path.write_bytes(b"x")

    def unexpected(_):
        pytest.fail("Over-budget output must not reach retention")

    with pytest.raises(TychoraError) as error:
        parser_process(
            BatchParser(),
            (path,),
            limits=ParserExecutionLimits(1, 5, 4294967296, 1, ExecutionContext()),
            progress=unexpected,
        )
    assert error.value.code == "LIMIT_EXCEEDED"
    assert not multiprocessing.active_children()


def test_missing_rss_support_fails_before_spawn(tmp_path, monkeypatch):
    import psutil

    from tychora.application.runtime import ParserExecutionLimits, parser_process

    monkeypatch.setattr(psutil, "__version__", "unqualified")
    with pytest.raises(TychoraError) as error:
        parser_process(
            BatchParser(),
            (),
            limits=ParserExecutionLimits(1, 5, 4294967296, 100, ExecutionContext()),
        )
    assert error.value.code == "CAPABILITY_MISSING"
    assert not multiprocessing.active_children()


def test_sampled_rss_threshold_reaps_an_entered_child(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import psutil

    from tychora.application.runtime import ParserExecutionLimits, parser_process

    marker = tmp_path / "parser-child-live"
    path = tmp_path / "input"
    path.write_text(str(tmp_path))

    class Monitor:
        def memory_info(self):
            return SimpleNamespace(rss=2 if marker.exists() else 0)

    monkeypatch.setattr(psutil, "Process", lambda _: Monitor())
    with pytest.raises(TychoraError) as error:
        parser_process(
            blocked_parser,
            (path,),
            limits=ParserExecutionLimits(1, 10, 1, 100, ExecutionContext()),
        )
    assert error.value.code == "LIMIT_EXCEEDED"
    assert marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
    assert not multiprocessing.active_children()
