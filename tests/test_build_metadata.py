"""build_metadata: orchestration with fakes, and one real AdventureWorks run."""

import json

import pytest

from schemagraph.extraction import build_metadata as bm
from schemagraph.extraction import extract, infer
from tests.test_infer_unit import meta_of

SPEC = {"a": (1, [("aid", "integer", True)]), "b": (1, [("bid", "integer", True)])}


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    calls = {"extract": [], "infer": [], "saved": []}

    def fake_extract(db_id, *, sample_n=5):
        calls["extract"].append((db_id, sample_n))
        if db_id == "broken":
            raise extract.ExtractionError("boom")
        return meta_of(SPEC)

    def fake_infer(db_id, meta, **kw):
        calls["infer"].append((db_id, kw))
        return meta

    monkeypatch.setattr(extract, "extract_metadata", fake_extract)
    monkeypatch.setattr(infer, "with_inferred", fake_infer)
    monkeypatch.setattr(extract, "METADATA_DIR", tmp_path)
    return calls


def test_build_one_extracts_infers_and_saves(fakes, tmp_path):
    r = bm.build_one("good", sample_n=3, min_overlap=0.8)
    assert r.ok and r.path == tmp_path / "good.json"
    assert (r.tables, r.columns, r.declared, r.inferred) == (2, 2, 0, 0)
    assert fakes["extract"] == [("good", 3)]
    assert fakes["infer"][0][1] == {"min_overlap": 0.8, "min_confidence": 0.5}
    assert json.loads(r.path.read_text())["db_id"] == "d"  # the fake meta's id: file was really written


def test_skip_inference_never_calls_infer(fakes):
    assert bm.build_one("good", skip_inference=True).ok
    assert fakes["infer"] == []


def test_failure_is_captured_not_raised(fakes, tmp_path):
    r = bm.build_one("broken")
    assert not r.ok and "ExtractionError: boom" in r.error and r.path is None
    assert not (tmp_path / "broken.json").exists()


def test_one_failure_does_not_stop_the_batch(fakes):
    results = bm.build_many(["good", "broken", "other"])
    assert [r.ok for r in results] == [True, False, True]


def test_out_rejected_for_several_databases(fakes, tmp_path):
    with pytest.raises(ValueError):
        bm.build_many(["a", "b"], out=tmp_path / "x.json")


def test_format_results_summary(fakes):
    text = bm.format_results(bm.build_many(["good", "broken"]))
    assert "ok    good" in text and "FAIL  broken" in text and text.endswith("1 built, 1 failed")


def test_main_exit_codes_and_args(fakes, capsys):
    assert bm.main(["--db", "good", "--db", "good"]) == 0   # duplicates collapse
    assert fakes["extract"] == [("good", 5)]
    assert bm.main(["--db", "broken"]) == 1
    assert "FAIL  broken" in capsys.readouterr().out


def test_main_all_uses_list_databases(fakes, monkeypatch):
    monkeypatch.setattr(bm.db, "list_databases", lambda: ["x", "y"])
    assert bm.main(["--all"]) == 0
    assert [c[0] for c in fakes["extract"]] == ["x", "y"]


@pytest.mark.parametrize("argv", [[], ["--all", "--db", "x"]])
def test_main_requires_db_or_all(fakes, argv):
    with pytest.raises(SystemExit):
        bm.main(argv)


@pytest.mark.adventureworks
def test_adventureworks_end_to_end(tmp_path):
    out = tmp_path / "aw.json"
    (r,) = bm.build_many(["adventureworks"], out=out)
    assert r.ok, r.error
    assert (r.tables, r.columns, r.inferred) == (68, 456, 2)
    reloaded = extract.load_metadata(out)
    assert reloaded.check_integrity() == []
    assert sum(x.source == "inferred" for x in reloaded.relationships) == 2