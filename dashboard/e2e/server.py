"""Real HTTP application over a disposable synthetic corpus; no providers or mocks."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

import uvicorn

from tychora import Tychora
from tychora.configuration.resolve import resolve_config
from tychora.interfaces.http.app import create_app
from tychora.interfaces.http.auth import LocalAuth


class SyntheticCounter:
    model = "synthetic-output"
    tokenizer_id = "cl100k_base"

    def count(self, text: str) -> int:
        return len(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="tychora-dashboard-e2e-") as temporary:
        workspace = Path(temporary) / "knowledge"
        # Initialize the standard workspace before resolving its explicit offline configuration.
        with Tychora.open(workspace, token_counter=SyntheticCounter()):
            pass
        configuration = workspace / "workspace.toml"
        with configuration.open("a") as stream:
            stream.write("\n[configuration]\noffline = true\n")
        config = resolve_config(configuration, "local", {})
        with Tychora.open(
            workspace, config=config, token_counter=SyntheticCounter()
        ) as kb:
            assert kb._workspace.owner.config.values.offline
            reader = kb.scope.model_copy(update={"capabilities": frozenset({"read"})})
            app = create_app(
                kb._workspace.owner,
                kb._workspace.services,
                LocalAuth(
                    {
                        "synthetic-dashboard-owner": kb.scope,
                        "synthetic-dashboard-reader": reader,
                    },
                    authority=f"127.0.0.1:{args.port}",
                ),
            )
            uvicorn.run(
                app,
                host="127.0.0.1",
                port=args.port,
                log_level="warning",
                access_log=False,
            )


if __name__ == "__main__":
    main()
