from schemagraph.eval import question_set as qs
from schemagraph.models import Question


def q(id="aw-1", db="adventureworks", text="How many orders?", sql="SELECT count(*) FROM sales.salesorderheader",
      tier=1, split="test"):
    return Question(id=id, db_id=db, question=text, gold_sql=sql, tier=tier, split=split)


def test_clean_set_has_no_problems():
    assert qs.problems([q(), q("aw-2", text="How many customers?")]) == []


def test_duplicate_ids_and_texts():
    found = qs.problems([q(), q(), q("aw-2", text="  how MANY   orders? ")])
    assert any("duplicate id 'aw-1'" in p for p in found)
    assert any("aw-2: same question as aw-1" in p for p in found)


def test_same_text_on_different_databases_is_fine():
    assert qs.problems([q(), q("b-1", db="other")]) == []


def test_unsafe_or_broken_gold_is_reported_with_the_reason():
    found = qs.problems([q(sql="DELETE FROM t"), q("aw-2", text="b", sql="SELECT pg_sleep(9)"), q("aw-3", text="c", sql="SELECT *")])
    assert any("aw-1" in p and "DELETE" in p for p in found)
    assert any("aw-2" in p and "pg_sleep" in p for p in found)


def test_bad_db_id_and_empty_question():
    found = qs.problems([q(db="Bad-Db"), q("aw-2", text="  ")])
    assert any("db_id" in p for p in found) and any("empty question" in p for p in found)


def test_fewshot_overlap_by_id_and_by_text():
    shots = [q("aw-1", text="Something else"), q("fs-9", text="HOW many orders?")]
    found = qs.problems([q(), q("aw-2", text="how many orders?")], shots)
    assert any("aw-1" in p and "same id" in p for p in found)
    assert any("aw-2" in p and "same question text" in p for p in found)
    assert qs.problems([q("aw-3", text="Unrelated question")], shots) == []


def test_describe_counts_by_database_tier_and_split():
    text = qs.describe([q(), q("aw-2", text="b", tier=3, split="dev"), q("b-1", db="other", text="c", tier=2)])
    lines = {line.split()[0]: line.split()[1:] for line in text.splitlines()[1:]}
    assert lines["adventureworks"] == ["1", "0", "1", "1", "1", "2"]
    assert lines["other"] == ["0", "1", "0", "1", "0", "1"]
    assert lines["ALL"] == ["1", "1", "1", "2", "1", "3"]
    assert qs.describe([]) == "no questions"


def test_cli(tmp_path, capsys):
    good = tmp_path / "q.jsonl"
    good.write_text(q().model_dump_json() + "\n")
    shots = tmp_path / "s.jsonl"
    shots.write_text(q("fs-1", text="Different").model_dump_json() + "\n")
    assert qs.main([str(good), "--fewshot", str(shots)]) == 0
    assert "no problems found" in capsys.readouterr().out
    shots.write_text(q("fs-1").model_dump_json() + "\n")      # same question text as the test question
    assert qs.main([str(good), "--fewshot", str(shots)]) == 1
    assert "PROBLEM" in capsys.readouterr().out
