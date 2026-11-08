"""Load and rank explicitly approved development few-shot examples."""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from functools import lru_cache
from pathlib import Path

from schemagraph.config import get_config
from schemagraph.models import FewShotExample

log = logging.getLogger(__name__)
_WORD = re.compile(r"[a-z0-9_]+", re.IGNORECASE)


@lru_cache(maxsize=4)
def _load_examples(path: str, _modified_ns: int | None) -> tuple[FewShotExample, ...]:
    file_path = Path(path)
    if not file_path.exists():
        log.warning("few-shot examples file %s does not exist; continuing without examples", file_path)
        return ()

    examples = []
    for line_no, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            example = FewShotExample.model_validate_json(line)
        except ValueError as exc:
            raise ValueError(f"{file_path}:{line_no}: invalid few-shot example: {exc}") from exc
        if example.split != "dev":
            raise ValueError(f"{file_path}:{line_no}: few-shot examples must use split='dev', never 'test'")
        examples.append(example)
    return tuple(examples)


def retrieve_examples(
    db_id: str, question: str, k: int = 3, exclude_ids: Collection[str] = ()
) -> list[FewShotExample]:
    """Return the top lexical-overlap development examples for this database.

    `exclude_ids` are example ids that must not be returned. The evaluation passes the id of the question it is
    asking: a dev question is also a few-shot example, and without this it would be its own best match, so the
    model would be shown the question with its gold SQL.
    """
    if k <= 0 or not question.strip():
        return []
    path = str(get_config().pipeline.fewshot_path)
    file_path = Path(path)
    modified_ns = file_path.stat().st_mtime_ns if file_path.exists() else None
    candidates = [example for example in _load_examples(path, modified_ns) if example.db_id == db_id and example.id not in exclude_ids]
    query_words = set(_WORD.findall(question.lower()))

    def rank(example: FewShotExample) -> tuple[float, str]:
        words = set(_WORD.findall(example.question.lower()))
        score = len(query_words & words) / len(query_words | words) if query_words | words else 0.0
        return (-score, example.id)

    return sorted(candidates, key=rank)[:k]
