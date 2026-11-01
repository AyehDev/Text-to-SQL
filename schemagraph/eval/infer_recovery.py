"""Measure inference by hiding foreign keys we know exist and seeing how many come back.

Declared foreign keys are ground truth. Take a database's metadata, remove a
random share of the single-column ones, run inference on what is left, and count:

  recovered     hidden relationships that inference found again
  wrong target  hidden sources where inference chose a different target
  extra         predictions for columns that never had a hidden relationship;
                these are either mistakes or genuinely undeclared relationships,
                so they need a human look rather than a verdict
  findable      hidden relationships the name/type rules can reach at all
                (the ceiling for `recovered`)

Hiding means filtering the metadata object, so nothing in the database changes.

    python -m schemagraph.eval.infer_recovery --db adventureworks \\
        --meta data/metadata/adventureworks.json --fraction 0.3 --seeds 10
"""

from __future__ import annotations

import argparse
import logging
import random
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from schemagraph.extraction import extract, infer
from schemagraph.extraction.infer import OverlapCounts
from schemagraph.models import DatabaseMetadata

Pair = tuple[str, str]  # (from_column UID, to_column UID)


@dataclass
class RecoveryResult:
    seed: int
    hidden: set[Pair]
    findable: set[Pair]
    recovered: set[Pair]
    wrong_target: list[tuple[Pair, str]] = field(default_factory=list)  # (hidden pair, chosen target)
    extra: list[Pair] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return len(self.recovered) / len(self.hidden) if self.hidden else 1.0


def single_column_declared(meta: DatabaseMetadata) -> list[Pair]:
    """Declared relationships whose constraint has exactly one column pair."""
    table_of = {c.uid: c.table_uid for c in meta.columns}
    groups: dict[tuple[str, str | None], list[Pair]] = {}
    for r in meta.relationships:
        if r.source == "declared":
            groups.setdefault((table_of[r.from_column], r.constraint_name), []).append(
                (r.from_column, r.to_column)
            )
    return sorted(pairs[0] for pairs in groups.values() if len(pairs) == 1)


def hide(meta: DatabaseMetadata, fraction: float, seed: int) -> tuple[DatabaseMetadata, set[Pair]]:
    """Remove a seeded random `fraction` of the single-column declared relationships."""
    candidates = single_column_declared(meta)
    chosen = set(random.Random(seed).sample(candidates, round(fraction * len(candidates))))
    kept = [r for r in meta.relationships if (r.from_column, r.to_column) not in chosen]
    return meta.model_copy(update={"relationships": kept}), chosen


def cached(fn: Callable[[str, str], OverlapCounts]) -> Callable[[str, str], OverlapCounts]:
    """Remember overlap results, so many seeds do not repeat the same queries."""
    memo: dict[Pair, OverlapCounts] = {}

    def wrapper(source: str, target: str) -> OverlapCounts:
        if (source, target) not in memo:
            memo[(source, target)] = fn(source, target)
        return memo[(source, target)]

    return wrapper


def run_recovery(
    db_id: str,
    meta: DatabaseMetadata,
    fraction: float,
    seeds: list[int],
    overlap_fn: Callable[[str, str], OverlapCounts] | None = None,
) -> list[RecoveryResult]:
    if overlap_fn is None:
        tables = {t.uid: t for t in meta.tables}
        refs = {
            c.uid: extract.ColumnRef(tables[c.table_uid].schema_, tables[c.table_uid].name, c.name, c.data_type)
            for c in meta.columns
        }
        overlap_fn = lambda s, t: infer.overlap_counts(db_id, refs[s], refs[t])  # noqa: E731
    overlap_fn = cached(overlap_fn)

    results = []
    for seed in seeds:
        hidden_meta, hidden = hide(meta, fraction, seed)
        reachable = {(p.from_column, p.to_column) for p in infer.find_candidate_pairs(hidden_meta)}
        predictions = infer.infer_relationships(db_id, hidden_meta, overlap_fn=overlap_fn)
        truth = dict(hidden)  # from_column -> hidden target

        result = RecoveryResult(seed=seed, hidden=hidden, findable=hidden & reachable, recovered=set())
        for r in predictions:
            pair = (r.from_column, r.to_column)
            if pair in hidden:
                result.recovered.add(pair)
            elif r.from_column in truth:
                result.wrong_target.append(((r.from_column, truth[r.from_column]), r.to_column))
            else:
                result.extra.append(pair)
        results.append(result)
    return results


def summarize(results: list[RecoveryResult]) -> dict[str, float]:
    recalls = [r.recall for r in results]
    return {
        "runs": len(results),
        "hidden_per_run": statistics.mean(len(r.hidden) for r in results),
        "recall_mean": statistics.mean(recalls),
        "recall_min": min(recalls),
        "recall_max": max(recalls),
        "ceiling_mean": statistics.mean(
            len(r.findable) / len(r.hidden) if r.hidden else 1.0 for r in results
        ),
        "wrong_target_per_run": statistics.mean(len(r.wrong_target) for r in results),
        "extra_per_run": statistics.mean(len(r.extra) for r in results),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hide declared FKs and measure how many inference recovers.")
    parser.add_argument("--db", required=True)
    parser.add_argument("--meta", type=Path, help="metadata JSON (default: data/metadata/<db>.json)")
    parser.add_argument("--fraction", type=float, default=0.3, help="share of FKs to hide (1.0 = all)")
    parser.add_argument("--seeds", type=int, default=10)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    meta = extract.load_metadata(args.meta or extract.default_metadata_path(args.db))
    results = run_recovery(args.db, meta, args.fraction, list(range(args.seeds)))
    s = summarize(results)

    short = lambda uid: uid.split(":", 1)[1]  # noqa: E731
    print(f"{args.db}: hid {args.fraction:.0%} of single-column declared FKs, {s['runs']} runs "
          f"(~{s['hidden_per_run']:.0f} hidden per run)")
    print(f"  recovered      mean {s['recall_mean']:.0%}  (min {s['recall_min']:.0%}, max {s['recall_max']:.0%}); "
          f"name rules can reach {s['ceiling_mean']:.0%}")
    print(f"  wrong target   {s['wrong_target_per_run']:.1f} per run")
    print(f"  extra          {s['extra_per_run']:.1f} per run")
    wrong = {(p, c) for r in results for p, c in r.wrong_target}
    extra = {p for r in results for p in r.extra}
    for (f, t), chosen in sorted(wrong):
        print(f"    wrong: {short(f)} -> {short(chosen)}   (truth: {short(t)})")
    for f, t in sorted(extra):
        print(f"    extra: {short(f)} -> {short(t)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())