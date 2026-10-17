import multiprocessing
import os
import time
from threading import Timer

import pytest

from tests.acceptance.parser_crash_probe import blocked_parser
from tychora.contracts.errors import TychoraError
from tychora.ports.execution import ExecutionContext


class WrappedPdfParser:
    """Exercise the real SDK error wrapper without constructing models/backends."""

    def __init__(self, fault="lineage"):
        self.fault = fault

    def _convert(self, call, paths, report):
        from pathlib import PurePath

        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.document import InputDocument
        from docling.datamodel.pipeline_options import PipelineOptions
        from docling.pipeline.base_pipeline import BasePipeline

        from tychora.adapters.docling.ocr_lineage_capture import invalid

        def assemble(result):
            if self.fault == "untyped":
                raise ValueError("Synthetic SDK details must remain private")
            if self.fault == "unsupported":
                raise TychoraError("FORBIDDEN_SCOPE", "Synthetic unapproved code")
            if self.fault == "implicit":
                try:
                    invalid()
                except TychoraError:
                    raise RuntimeError("Synthetic unrelated wrapper") from None
            invalid()

        class SyntheticPipeline(BasePipeline):
            def _build_document(self, conv_res):
                return conv_res

            def _assemble_document(self, conv_res):
                return assemble(conv_res)

            def _determine_status(self, conv_res):
                return conv_res.status

            @classmethod
            def get_default_options(cls):
                return PipelineOptions()

            @classmethod
            def is_backend_supported(cls, backend):
                return False

        pipeline = SyntheticPipeline(PipelineOptions())
        document = InputDocument.model_construct(
            file=PurePath("synthetic.pdf"),
            document_hash="a" * 64,
            format=InputFormat.PDF,
        )
        return BasePipeline.execute(pipeline, document, raises_on_error=True)

    def run(self, paths, report):
        from unittest.mock import patch

        from tychora.adapters.docling import pdf_batches
        from tychora.adapters.docling.parser import _Convert

        # Only replace model/backend work. The SDK wrapper, owning adapter and
        # runtime sanitization stay real, including in a separately spawned child.
        with patch.object(pdf_batches, "run_pdf_batches", self._convert):
            return _Convert("", "pdf").run(paths, report)

    def __call__(self, paths):
        return self._convert(None, paths, None)


@pytest.mark.parametrize(
    ("fault", "bounded", "expected"),
    [
        ("lineage", True, "INTEGRITY_ERROR"),
        ("untyped", True, "INVALID_REQUEST"),
        ("unsupported", True, "INVALID_REQUEST"),
        ("implicit", True, "INVALID_REQUEST"),
        ("lineage", False, "INVALID_REQUEST"),
    ],
)
def test_sdk_wrapped_pdf_failure_keeps_only_bounded_typed_code(
    fault, bounded, expected
):
    from tychora.application.runtime import _parser_entry

    receiver, sender = multiprocessing.Pipe()
    try:
        _parser_entry(WrappedPdfParser(fault), (), sender, bounded=bounded)
        assert receiver.recv() == ("error", expected)
        with pytest.raises(EOFError):
            receiver.recv()
    finally:
        receiver.close()
        sender.close()


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
    def __call__(self, paths):
        raise AssertionError("Batch parser requires the bounded report protocol")

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


def test_transient_rss_sampling_failures_are_retried_then_enforced(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    import psutil

    from tychora.application.runtime import ParserExecutionLimits, parser_process

    marker = tmp_path / "parser-child-live"
    path = tmp_path / "input"
    path.write_text(str(tmp_path))
    failures = []

    class Monitor:
        def memory_info(self):
            if len(failures) < 2:
                failures.append(1)
                raise psutil.AccessDenied()
            return SimpleNamespace(rss=2 if marker.exists() else 0)

    monkeypatch.setattr(psutil, "Process", lambda _: Monitor())
    with pytest.raises(TychoraError) as error:
        parser_process(
            blocked_parser,
            (path,),
            limits=ParserExecutionLimits(1, 10, 1, 100, ExecutionContext()),
        )
    assert len(failures) == 2
    assert error.value.code == "LIMIT_EXCEEDED"
    assert not multiprocessing.active_children()


def test_persistent_rss_sampling_failure_of_a_live_child_is_reported(
    tmp_path, monkeypatch
):
    import psutil

    from tychora.application.runtime import ParserExecutionLimits, parser_process

    path = tmp_path / "input"
    path.write_text(str(tmp_path))

    class Monitor:
        def memory_info(self):
            raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "Process", lambda _: Monitor())
    with pytest.raises(TychoraError) as error:
        parser_process(
            blocked_parser,
            (path,),
            limits=ParserExecutionLimits(1, 10, 1, 100, ExecutionContext()),
        )
    assert error.value.code == "CAPABILITY_MISSING"
    assert not multiprocessing.active_children()
