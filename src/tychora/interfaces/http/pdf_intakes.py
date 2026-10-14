"""HTTP composition for private PDF receipts."""

from fastapi import Request

from tychora.contracts.pdf_intake import PdfIntakeDescriptor, PdfIntakeSubmission
from tychora.ports.execution import ExecutionContext

from .routes import _json, _require, _scope


def register_pdf_intakes(app):
    @app.post("/v1/pdf-intakes")
    async def prepare(request: Request):
        _require(request, "write")
        descriptor = PdfIntakeDescriptor.model_validate_json(await request.body())
        return _json(
            await app.state.pdf_intakes.prepare(
                descriptor, _scope(request), ExecutionContext()
            ),
            status_code=201,
        )

    @app.put("/v1/pdf-intakes/{intake_id}/content")
    async def receive(intake_id: str, request: Request):
        _require(request, "write")
        return _json(
            await app.state.pdf_intakes.receive(
                intake_id, request.stream(), _scope(request), ExecutionContext()
            )
        )

    @app.post("/v1/pdf-intakes/{intake_id}/analyse")
    async def analyse(intake_id: str, request: Request):
        return _json(
            await app.state.pdf_intakes.analyse(
                intake_id, _scope(request), ExecutionContext()
            )
        )

    @app.post("/v1/pdf-intakes/{intake_id}/submit")
    async def submit(intake_id: str, request: Request):
        submission = PdfIntakeSubmission.model_validate_json(await request.body())
        return _json(
            await app.state.pdf_intakes.submit(
                intake_id,
                submission,
                _scope(request),
                request.headers.get("idempotency-key"),
                ExecutionContext(),
            ),
            status_code=202,
        )

    @app.get("/v1/pdf-intakes/{intake_id}")
    async def inspect(intake_id: str, request: Request):
        return _json(
            await app.state.pdf_intakes.inspect(
                intake_id, _scope(request), ExecutionContext()
            )
        )

    @app.delete("/v1/pdf-intakes/{intake_id}")
    async def abort(intake_id: str, request: Request):
        return _json(
            await app.state.pdf_intakes.abort(
                intake_id, _scope(request), ExecutionContext()
            )
        )
