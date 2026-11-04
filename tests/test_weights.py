"""Edge weights (SPEC 5.6): lower is cheaper, so the planner prefers declared joins
and avoids traversals that multiply rows."""

import pytest

from schemagraph.graph.weights import edge_weight, edge_weights


def test_declared_forward_is_the_cheapest_possible():
    assert edge_weight(1.0, "forward") == 1.0


def test_declared_reverse_pays_the_fan_out_penalty():
    assert edge_weight(1.0, "reverse") == 2.0


def test_inferred_edges_cost_more_the_less_confident_they_are():
    assert edge_weight(0.72, "forward") == 2.4
    assert edge_weight(0.72, "reverse") == 3.4
    assert edge_weight(0.95, "forward") < edge_weight(0.6, "forward")


def test_an_inferred_edge_never_beats_a_declared_one():
    # inference confidence is capped at 0.95, which must still cost more than declared
    assert edge_weight(0.95, "forward") > edge_weight(1.0, "forward")
    assert edge_weight(0.95, "reverse") > edge_weight(1.0, "reverse")


def test_no_float_noise():
    assert edge_weight(0.72, "forward") == 2.4  # 5 * (1 - 0.72) is 1.4000000000000001 in floats
    assert edge_weight(0.9, "forward") == 1.5


@pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan")])
def test_confidence_must_be_between_zero_and_one(bad):
    with pytest.raises(ValueError):
        edge_weight(bad, "forward")


def test_direction_must_be_known():
    with pytest.raises(ValueError):
        edge_weight(1.0, "sideways")


def test_many_to_one_edge_weights():
    assert edge_weights(1.0, "N:1") == (1.0, 2.0)
    assert edge_weights(0.72, "N:1") == (2.4, 3.4)


def test_one_to_one_reverse_does_not_fan_out():
    # the FK column is unique, so walking back from the parent yields at most one row
    assert edge_weights(1.0, "1:1") == (1.0, 1.0)
    assert edge_weights(0.72, "1:1") == (2.4, 2.4)


def test_unknown_cardinality_is_rejected():
    with pytest.raises(ValueError):
        edge_weights(1.0, "N:N")
