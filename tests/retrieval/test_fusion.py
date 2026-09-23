import pytest

from tychora.retrieval.fusion import rrf_score


def test_rrf_uses_one_vote_per_channel():
    assert rrf_score(
        {"lexical": 1, "text": 2}, {"lexical": 1.0, "text": 1.0}
    ) == pytest.approx(1 / 61 + 1 / 62)


from tychora.contracts.candidates import ChannelCandidate
from tychora.retrieval.fusion import fuse


def _hit(uid: str, rank: int, *windows: str) -> ChannelCandidate:
    return ChannelCandidate(key=("unit", uid), rank=rank, matching_window_ids=windows)


def test_many_text_families_collapse_to_one_dense_vote_and_best_owner_window():
    fused = fuse(
        {
            "text:one": (_hit("a", 8, "a-late"), _hit("a", 2, "a-best"), _hit("b", 5)),
            "text:two": (_hit("a", 1, "a-other"), _hit("b", 1)),
            "lexical": (_hit("a", 1),),
        },
        {"text": 1.0, "lexical": 1.0},
    )
    assert [item.key for item in fused] == [("unit", "a"), ("unit", "b")]
    assert fused[0].ranks == {"text": 1, "lexical": 1}
    assert fused[0].score == pytest.approx(2 / 61)
    assert fused[0].matching_window_ids == ("a-other",)
    assert fused[1].ranks == {"text": 2}


def test_equal_scores_use_typed_id_and_hints_are_capped():
    fused = fuse(
        {"visual": (_hit("z", 1), ChannelCandidate(key=("asset", "a"), rank=1))},
        {"visual": 0.7},
        hints={("unit", "z"): 3.0},
        hints_enabled=True,
    )
    assert fused[0].key == ("unit", "z")
    assert fused[0].hint == pytest.approx(0.01)
    assert fused[1].key == ("asset", "a")
    assert fuse(
        {"visual": (_hit("z", 1), ChannelCandidate(key=("asset", "a"), rank=1))},
        {"visual": 0.7},
    )[0].key == ("asset", "a")
