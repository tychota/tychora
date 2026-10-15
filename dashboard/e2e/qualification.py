"""Opt-in disposable fixtures: real assets/services, explicitly simulated transport."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from tychora.adapters.voyage.request_types import RawVoyageResponse


def prepare(scenario: str, workspace: Path, fixtures: Path) -> None:
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "context.json").write_text(
        json.dumps(
            {
                "backup": str(workspace.parent / "backup"),
                "restore": str(workspace.parent / "restored"),
            }
        )
    )
    if scenario == "remote":
        if not os.environ.get("TYCHORA_MODEL_CACHE"):
            raise RuntimeError(
                "Remote simulation requires explicit verified local tokenizer cache"
            )
        from tychora.application import default_bindings

        os.environ["TYCHORA_SMOKE_VOYAGE_KEY"] = "synthetic-transport-only"
        record = fixtures / "simulated-provider.jsonl"
        record.write_text("")

        class SimulatedTransport:
            def __init__(self, *, api_key):
                if api_key != "synthetic-transport-only":
                    raise RuntimeError(
                        "Only the synthetic fixture credential is allowed"
                    )

            def post_json(self, path, body, timeout):
                payload = json.loads(body)
                if path == "/v1/contextualizedembeddings":
                    data = [
                        {
                            "index": outer,
                            "data": [
                                {"index": inner, "embedding": [1.0] + [0.0] * 1023}
                                for inner, _ in enumerate(batch)
                            ],
                        }
                        for outer, batch in enumerate(payload["inputs"])
                    ]
                elif path == "/v1/rerank":
                    data = [
                        {"index": i, "relevance_score": 1.0 / (i + 1)}
                        for i, _ in enumerate(payload["documents"])
                    ]
                else:
                    raise RuntimeError("Unexpected simulated provider endpoint")
                with record.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {"simulation": True, "path": path, "payload": payload}
                        )
                        + "\n"
                    )
                return RawVoyageResponse(200, json.dumps({"data": data}).encode())

            def close(self):
                pass

        # The disposable server process owns the deliberately simulated transport.
        patch.object(
            default_bindings, "HttpxVoyageTransport", SimulatedTransport
        ).start()
    elif scenario == "ocr":
        if not os.environ.get("TYCHORA_TEST_DOCLING_MODELS") or not os.environ.get(
            "TYCHORA_TEST_OCR_MODELS"
        ):
            raise RuntimeError(
                "OCR qualification requires explicit Docling and OCR model prerequisites"
            )
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from tests.qualification.test_scanned_pdf import generated_scan

        (fixtures / "synthetic-scan.pdf").write_bytes(generated_scan("columns"))
        try:
            from dashboard.e2e.pdf_fixture import generate_pdf
        except ModuleNotFoundError:
            from pdf_fixture import generate_pdf
        for mode in ("mixed", "scan", "native", "blank"):
            (fixtures / f"task6-{mode}.pdf").write_bytes(generate_pdf(mode))
        (fixtures / "task6-invalid.pdf").write_bytes(b"not a PDF")
        pack = workspace / "browser-profile"
        pack.mkdir()
        modes = ""
        for name, policy in (
            ("review-excluded", "accepted_only"),
            ("review-included", "permitted"),
        ):
            modes += f'''\n[modes.{name}]
allowed_lanes=["authority","standard","feedback","evidence","example","maintenance"]
allowed_uses=["lookup"]
original_review_states=["unreviewed","accepted"]
derived_proxy_policy="{policy}"
allow_derived_proxies=true
derived_proxy_review_states={('["accepted"]' if policy == "accepted_only" else '["needs_review","accepted"]')}
required_lanes=[]
'''
        (pack / "profile.toml").write_text(
            'schema_version=1\nid="browser-review"\nversion="1"\nextends=["builtin:general-documents@1"]\n'
            + modes
        )
        (workspace / "workspace.toml").write_text(
            'schema_version=1\n[profiles.local]\npath="browser-profile"\n[configuration]\noffline=true\ndefault_mode="lookup"\n'
        )
