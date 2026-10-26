"""Explicit opt-in shape check using a labelled synthetic diagram only."""

import math
import os
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from tests.helpers.assets import asset_path
from tests.helpers.corpus import COLLECTION
from tests.providers.test_visual import _descriptor, _install
from tychora.adapters.voyage.request import HttpxVoyageTransport
from tychora.adapters.voyage.runtime import VoyageRuntime
from tychora.adapters.voyage.validation import qualify_voyage_visual_capabilities
from tychora.application.owner import WorkspaceOwner
from tychora.application.setup import (
    load_voyage_multimodal_accounting,
    load_voyage_tokenizer,
)
from tychora.application.visual import create_visual_embedder
from tychora.configuration.resolve import resolve_config
from tychora.contracts.embeddings import TextQuery
from tychora.contracts.requests import RequestScope
from tychora.ports.execution import ExecutionContext

pytestmark = pytest.mark.live


class ProvisionedCounter:
    model = "voyage-multimodal-3.5"
    tokenizer_id = "voyage-multimodal-3.5:live-qualified"

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    def count(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)


def _live_enabled():
    return (
        os.environ.get("TYCHORA_RUN_VOYAGE_VISUAL_LIVE") == "1"
        and bool(os.environ.get("VOYAGE_API_KEY"))
        and bool(os.environ.get("TYCHORA_TEST_MODEL_CACHE"))
    )


def _diagram():
    image = Image.new("RGB", (256, 256), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 75, 90, 160), fill="#205aa8")
    draw.rectangle((165, 75, 235, 160), fill="#c84f35")
    draw.line((93, 118, 160, 118), fill="black", width=5)
    draw.polygon(((160, 118), (145, 109), (145, 127)), fill="black")
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


@pytest.mark.assets("TYCHORA_TEST_MODEL_CACHE")
@pytest.mark.skipif(
    not _live_enabled(),
    reason="requires explicit visual live opt-in, credential, and provisioned model cache",
)
@pytest.mark.asyncio
async def test_live_synthetic_diagram_and_distinctly_phrased_text_query(tmp_path):
    cache = asset_path("TYCHORA_TEST_MODEL_CACHE")
    counter = ProvisionedCounter(load_voyage_tokenizer(cache, "voyage-multimodal-3.5"))
    capabilities = qualify_voyage_visual_capabilities(
        counter=counter, limits=load_voyage_multimodal_accounting(cache)
    )
    config_path = tmp_path / "workspace.toml"
    config_path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    config = resolve_config(config_path, "test", {})
    config = config.model_copy(
        update={"values": config.values.model_copy(update={"offline": False})}
    )
    scope = RequestScope(
        principal_id="synthetic-live-principal",
        collection_ids=frozenset({COLLECTION}),
        access_ceiling=2,
        uses=frozenset({"lookup"}),
        upload_providers=frozenset({"voyage"}),
    )
    with WorkspaceOwner.open(tmp_path / "data", config, scope) as owner:
        bundle, asset, ref, image = await owner.run_write(
            lambda: _install(
                owner,
                caption="A blue rectangle points toward a red rectangle.",
                image=_diagram(),
            ),
            ExecutionContext(),
        )
        runtime = VoyageRuntime(
            lambda: HttpxVoyageTransport(api_key=os.environ["VOYAGE_API_KEY"]),
            workspace_id=owner.workspace_id,
            credential_binding_id="explicit-live-visual-credential",
        )
        ctx = ExecutionContext()
        provider = await create_visual_embedder(
            owner,
            capabilities=capabilities,
            counter=counter,
            runtime=runtime,
            scope=scope,
            ctx=ctx,
        )
        item = _descriptor(bundle, asset, ref, image)
        (document,) = await owner.embed_visual_assets(
            provider, (item,), ctx, scope=scope
        )
        question = await owner.embed_visual_query(
            provider,
            TextQuery(text="Which colored box does the left shape lead to?"),
            ctx,
            scope=scope,
        )
        assert document.space_id == question.space_id == capabilities.space_id
        assert len(document.values) == len(question.values) == 1024
        assert all(
            math.isfinite(value) for value in (*document.values, *question.values)
        )
