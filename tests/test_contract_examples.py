"""The sample files in contracts/examples/ are what teammates build against (the frontend mock,
pipeline stubs). These tests keep them valid and consistent with each other, so a change to
models.py that breaks a sample fails here instead of in someone else's branch.

If a sample must change on purpose, regenerate it and update the matching test.
"""

import json
import uuid
from pathlib import Path

import pytest

from schemagraph.extraction import extract
from schemagraph.models import AskResponse, JoinPlan, display_name
from schemagraph.sql_validation import check_select, referenced_tables

EXAMPLES = Path(__file__).parent.parent / "contracts" / "examples"


def load(name):
    return json.loads((EXAMPLES / name).read_text())


@pytest.fixture(scope="module")
def response():
    return AskResponse.model_validate(load("ask_response.sample.json"))


@pytest.fixture(scope="module")
def plan():
    return JoinPlan.model_validate(load("join_plan.sample.json"))


def test_every_sample_parses():
    meta = extract.load_metadata(EXAMPLES / "metadata.adventureworks.sample.json")
    assert meta.db_id == "adventureworks"
    JoinPlan.model_validate(load("join_plan.sample.json"))
    AskResponse.model_validate(load("ask_response.sample.json"))


def test_response_round_trips_without_losing_fields(response):
    # extra="forbid" already rejects unknown keys; this catches a field dropped on the way back out
    assert json.loads(response.model_dump_json(by_alias=True)) == load("ask_response.sample.json")


def test_response_uses_the_wire_names_the_frontend_reads():
    raw = load("ask_response.sample.json")
    assert {"from", "to", "source", "text"} == set(raw["explanation"]["join_path"][0])   # "from", not "from_"
    assert uuid.UUID(raw["request_id"])


def test_embedded_join_plan_is_the_join_plan_sample(response, plan):
    assert response.join_plan == plan


def test_explanation_tables_agree_with_the_plan(response, plan):
    tables = {t.uid: t for t in response.explanation.tables}
    assert set(tables) == set(plan.tables)
    for uid, t in tables.items():
        assert t.display_name == display_name(uid)
        expected = "seed" if uid in {s.uid for s in plan.seed_tables} else "bridge"
        assert t.role == expected
    assert {u for u, t in tables.items() if t.role == "bridge"} == set(plan.bridge_tables)


def test_join_path_agrees_with_the_plan(response, plan):
    got = [(j.from_, j.to, j.source) for j in response.explanation.join_path]
    want = [(display_name(j.from_column), display_name(j.to_column), j.source) for j in plan.joins]
    assert got == want


def test_sql_is_a_safe_select_over_exactly_the_planned_tables(response, plan):
    assert check_select(response.sql).ok
    assert referenced_tables(response.sql) == {display_name(u) for u in plan.tables}
    assert [a.sql for a in response.attempts] == [response.sql] and response.attempts[0].error is None


def test_result_is_consistent(response):
    r = response.result
    assert r.row_count == len(r.rows) and not r.truncated
    assert all(len(row) == len(r.columns) for row in r.rows)
    assert all(v is None or isinstance(v, str) for row in r.rows for v in row)   # every value a string or null


def test_metrics_are_consistent(response):
    m, lat = response.metrics, response.metrics.latency_ms
    assert m.attempt_count == len(response.attempts)
    assert lat.total == lat.retrieval + lat.planning + lat.generation + lat.validation + lat.execution + lat.explanation


def test_success_response_has_the_mode_specific_fields(response):
    assert response.status == "success" and response.mode == "graph"
    assert response.join_plan is not None and response.explanation is not None
    assert response.db_id == response.join_plan.db_id
    # the spec fills `interpretation` only when the plan has alternatives; this plan has none
    assert bool(response.explanation.interpretation) == bool(response.join_plan.alternatives)
