"""Edge weights for the join planner (SPEC 5.6). Pure functions, no Neo4j.

    weight = 1 + 5 * (1 - confidence) + (1 if the traversal fans out else 0)

A declared foreign key has confidence 1.0, so walking it from the child to the
parent (forward, many-to-one) costs 1.0. Walking it back from the parent to the
children (reverse, one-to-many) costs 2.0: it can multiply rows, so the planner
should only choose it when it has to. A weaker inferred edge costs more in both
directions, so declared joins win ties.

Every edge is stored once, pointing from the FK column to the referenced
column. `weight_forward` is the cost of walking with the arrow and
`weight_reverse` the cost of walking against it.
"""

from __future__ import annotations

import math
from typing import Literal

Direction = Literal["forward", "reverse"]

CONFIDENCE_PENALTY = 5.0     # cost of going from certain (1.0) to a pure guess (0.0)
FAN_OUT_PENALTY = 1.0        # extra cost of a one-to-many traversal


def edge_weight(confidence: float, direction: Direction) -> float:
    """Cost of walking one relationship edge in one direction."""
    if not (isinstance(confidence, (int, float)) and 0.0 <= confidence <= 1.0) or math.isnan(confidence):
        raise ValueError(f"confidence must be between 0 and 1, got {confidence!r}")
    if direction not in ("forward", "reverse"):
        raise ValueError(f"direction must be 'forward' or 'reverse', got {direction!r}")
    fan_out = FAN_OUT_PENALTY if direction == "reverse" else 0.0
    # rounding hides float noise such as 5 * (1 - 0.72) == 1.4000000000000001
    return round(1.0 + CONFIDENCE_PENALTY * (1.0 - confidence) + fan_out, 6)


def edge_weights(confidence: float, cardinality: str) -> tuple[float, float]:
    """(weight_forward, weight_reverse) for a relationship.

    For a 1:1 relationship the FK column is unique, so walking back from the
    parent reaches at most one row and does not fan out: reverse costs the same
    as forward.
    """
    if cardinality not in ("N:1", "1:1"):
        raise ValueError(f"cardinality must be 'N:1' or '1:1', got {cardinality!r}")
    forward = edge_weight(confidence, "forward")
    reverse = forward if cardinality == "1:1" else edge_weight(confidence, "reverse")
    return forward, reverse