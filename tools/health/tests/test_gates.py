"""The registry holds every gate of the design, grouped for pull requests and nightly."""

import pytest

from tools.health.gates import GATES, select


def test_every_spec_gate_is_registered():
    ids = {gate.id for gate in GATES}
    expected = ({f"G{i}" for i in range(1, 29)} - {"G9", "G10", "G11"}) | {"G9-G11"}
    assert ids == expected


def test_fast_selection_is_the_local_subset():
    assert [gate.id for gate in select("pr", fast=True)] == [
        "G1",
        "G2",
        "G3",
        "G7",
        "G15",
    ]


def test_nightly_group_holds_branch_coverage_mutation_and_gold():
    assert [(gate.id, gate.title) for gate in select("nightly")] == [
        ("G16", "Coverage, branches"),
        ("G21", "Mutation score"),
        ("G22", "Code gold set"),
    ]


def test_unknown_gate_is_rejected():
    with pytest.raises(SystemExit, match="G99"):
        select("pr", "G99")
