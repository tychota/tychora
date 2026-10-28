"""A request narrows the configured channels, reranking and review states."""

import pytest

from tests.helpers.legacy import OutputCounter
from tests.helpers.owner import owner_row
from tychora import Tychora
from tychora.application.query_plan import QueryBindings, plan_for
from tychora.configuration.resolve import resolve_config
from tychora.contracts.errors import TychoraError
from tychora.contracts.requests import QueryRequest
from tychora.retrieval.compiler import request_digest
from tychora.retrieval.query import result_cache_key


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "workspace.toml"
    path.write_text('schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n')
    return resolve_config(path, "test", {})


@pytest.fixture
def image_config(tmp_path):
    path = tmp_path / "image.toml"
    path.write_text(
        'schema_version=1\n[profiles.test]\nbuiltin="documents@1"\n'
        '[configuration.providers.image]\nprovider="voyage"\n'
        'model="voyage-multimodal-3.5"\ndimensions=1024\n'
        '[configuration.index.image]\nbackend="chroma"\n'
        "[configuration.retrieval.graph]\nsemantic=true\n"
    )
    return resolve_config(path, "test", {})


def test_channels_can_only_narrow_configuration(config):
    request = QueryRequest.from_mapping(
        {"question": "seal", "channels": {"text": True}}
    )
    with pytest.raises(TychoraError) as error:
        plan_for(request, config, QueryBindings())
    assert (error.value.code, error.value.reason) == (
        "INVALID_REQUEST",
        "channel.not_configured",
    )
    plan = plan_for(
        QueryRequest.from_mapping({"question": "seal"}), config, QueryBindings()
    )
    assert plan.lexical and not plan.text and not plan.image


def test_a_channel_turned_off_is_skipped(config):
    request = QueryRequest.from_mapping(
        {"question": "seal", "channels": {"lexical": False}}
    )
    plan = plan_for(request, config, QueryBindings())
    assert not plan.lexical
    assert plan.skipped == ("lexical",)


def test_rerank_off_skips_rerank_stage(config):
    off = plan_for(
        QueryRequest.from_mapping({"question": "seal", "rerank": "off"}),
        config,
        QueryBindings(),
    )
    assert not off.rerank
    with pytest.raises(TychoraError) as error:
        plan_for(
            QueryRequest.from_mapping({"question": "seal", "rerank": "on"}),
            config,
            QueryBindings(),
        )
    assert error.value.reason == "channel.not_configured"


def test_a_configured_but_unbound_channel_stays_in_the_default_plan(image_config):
    plan = plan_for(
        QueryRequest.from_mapping({"question": "seal"}), image_config, QueryBindings()
    )
    assert plan.image and plan.graph_strategies == frozenset({"semantic"})
    assert plan.explicit == frozenset() and plan.skipped == ()
    for channels in ({"image": True}, {"graph": True}):
        request = QueryRequest.from_mapping({"question": "seal", "channels": channels})
        with pytest.raises(TychoraError) as error:
            plan_for(request, image_config, QueryBindings())
        assert (error.value.code, error.value.reason) == (
            "INVALID_REQUEST",
            "channel.not_configured",
        )


def test_explicit_holds_only_the_channels_the_request_selected(image_config):
    bindings = QueryBindings(
        visual_provider=object(), visual_index=object(), graph=object()
    )
    request = QueryRequest.from_mapping(
        {
            "question": "seal",
            "modalities": ["text", "image"],
            "channels": {
                "image": True,
                "lexical": False,
                "graph_strategies": ["semantic"],
            },
        }
    )
    plan = plan_for(request, image_config, bindings)
    assert plan.explicit == frozenset({"image", "graph"})
    assert plan.skipped == ("lexical",)
    assert plan.graph_strategies == frozenset({"semantic"})


def test_an_explicit_channel_the_query_cannot_use_is_refused(image_config):
    bindings = QueryBindings(visual_provider=object(), visual_index=object())
    lexical_only = {"question": "seal", "modalities": ["lexical"]}
    assert plan_for(
        QueryRequest.from_mapping(lexical_only), image_config, bindings
    ).image
    request = QueryRequest.from_mapping({**lexical_only, "channels": {"image": True}})
    with pytest.raises(TychoraError) as error:
        plan_for(request, image_config, bindings)
    assert error.value.code == "INVALID_REQUEST"


def test_review_states_change_request_digest():
    plain = QueryRequest.from_mapping({"question": "seal"})
    accepted = QueryRequest.from_mapping(
        {"question": "seal", "review_states": ["accepted"]}
    )
    other_question = QueryRequest.from_mapping({"question": "valve"})
    assert request_digest(plain, None) != request_digest(accepted, None)
    assert request_digest(plain, None) == request_digest(other_question, None)


def test_each_new_request_field_changes_the_result_cache_key():
    plain = {"question": "seal"}
    requests = [
        plain,
        {**plain, "channels": {"graph": False}},
        {**plain, "channels": {"graph_strategies": ["semantic", "structural"]}},
        {**plain, "rerank": "off"},
        {**plain, "rerank": "on"},
        {**plain, "review_states": ["accepted"]},
    ]
    keys = {
        result_cache_key(QueryRequest.from_mapping(request), (), 0, "config")
        for request in requests
    }
    assert len(keys) == len(requests)


def _add(kb, key, text, approval):
    job = kb.sources.add(
        {
            "type": "text",
            "source_key": key,
            "collection_id": "default",
            "kind": "document@1",
            "format": "markdown",
            "text": text,
            "metadata": {"title": key, "approval": approval},
        }
    )
    assert kb.jobs.wait(job.id).state == "succeeded"


def test_review_states_narrow_mode_states(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "accepted.md", "# A\n\nSeal inspection accepted.\n", "accepted")
        _add(kb, "draft.md", "# B\n\nSeal inspection draft.\n", "unreviewed")
        everything = kb.search({"question": "seal inspection"})
        narrowed = kb.search(
            {"question": "seal inspection", "review_states": ["accepted"]}
        )
    assert {s.source_id for s in everything.segments} > {
        s.source_id for s in narrowed.segments
    }
    assert len({s.source_id for s in narrowed.segments}) == 1


def test_a_turned_off_channel_is_reported_skipped(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "accepted.md", "# A\n\nSeal inspection accepted.\n", "accepted")
        plain = kb.search({"question": "seal inspection"})
        narrowed = kb.search(
            {
                "question": "seal inspection",
                "channels": {"graph": False},
                "rerank": "off",
            }
        )
    assert plain.coverage.skipped_channels == ()
    assert narrowed.coverage.skipped_channels == ("graph", "rerank")
    assert narrowed.coverage.status == "complete"
    assert narrowed.segments


def test_generation_policy_never_generate_excluded_from_compose(tmp_path):
    with Tychora.open(tmp_path / "ws", token_counter=OutputCounter()) as kb:
        _add(kb, "kept.md", "# A\n\nSeal inspection kept.\n", "accepted")
        _add(kb, "reference.md", "# B\n\nSeal inspection reference.\n", "accepted")
        owner = kb._workspace.owner
        row = owner_row(
            owner, "SELECT source_id FROM sources WHERE source_key='reference.md'"
        )
        assert row is not None
        (source_id,) = row
        kb.sources.set_allowed_uses(source_id, "never_generate")
        composed = kb.search({"question": "seal inspection", "mode": "compose"})
        looked_up = kb.search({"question": "seal inspection", "mode": "lookup"})
    assert source_id not in {s.source_id for s in composed.segments}
    assert source_id in {s.source_id for s in looked_up.segments}
