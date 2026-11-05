"""Extract, infer and save metadata for one or more databases in a single command.

    python -m schemagraph.extraction.build_metadata --db adventureworks
    python -m schemagraph.extraction.build_metadata --db a --db b --skip-inference
    python -m schemagraph.extraction.build_metadata --all

This is the hand-off point to graph ingestion: it writes data/metadata/<db_id>.json,
the file the graph builder reads. One database failing does not stop the others;
the exit code is 1 if any failed.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from schemagraph import db
from schemagraph.extraction import extract, infer
from schemagraph.models import DatabaseMetadata

log = logging.getLogger(__name__)


@dataclass
class BuildResult:
    db_id: str
    path: Path | None = None
    tables: int = 0
    columns: int = 0
    declared: int = 0
    inferred: int = 0
    seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def build_one(
    db_id: str,
    *,
    out: Path | None = None,
    skip_inference: bool = False,
    sample_n: int = 5,
    min_overlap: float = infer.MIN_OVERLAP,
    min_confidence: float = 0.5,
) -> BuildResult:
    """Build and save one database's metadata. Errors are captured, not raised."""
    start = time.monotonic()
    result = BuildResult(db_id)
    try:
        meta: DatabaseMetadata = extract.extract_metadata(db_id, sample_n=sample_n)
        if not skip_inference:
            meta = infer.with_inferred(
                db_id, meta, min_overlap=min_overlap, min_confidence=min_confidence
            )
        path = out or extract.default_metadata_path(db_id)
        extract.save_metadata(meta, path)
        result.path = path
        result.tables, result.columns = len(meta.tables), len(meta.columns)
        result.declared = sum(r.source == "declared" for r in meta.relationships)
        result.inferred = sum(r.source == "inferred" for r in meta.relationships)
    except Exception as exc:  # noqa: BLE001 - reported per database, not fatal to the batch
        log.debug("build failed for %s", db_id, exc_info=True)
        result.error = f"{type(exc).__name__}: {exc}"
    result.seconds = time.monotonic() - start
    return result


def build_many(db_ids: list[str], **kwargs) -> list[BuildResult]:
    if kwargs.get("out") is not None and len(db_ids) > 1:
        raise ValueError("--out only makes sense with a single database")
    return [build_one(d, **kwargs) for d in db_ids]


def format_results(results: list[BuildResult]) -> str:
    lines = []
    for r in results:
        if r.ok:
            lines.append(
                f"  ok    {r.db_id:<28} {r.tables:>4} tables {r.columns:>5} columns  "
                f"{r.declared:>4} declared + {r.inferred} inferred  ({r.seconds:.1f}s)"
            )
        else:
            lines.append(f"  FAIL  {r.db_id:<28} {r.error}")
    failed = sum(not r.ok for r in results)
    lines.append(f"{len(results) - failed} built, {failed} failed")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract + infer + save metadata for databases.")
    parser.add_argument("--db", action="append", default=[], help="db_id (repeatable)")
    parser.add_argument("--all", action="store_true", help="every database the read-only role can see")
    parser.add_argument("--out", type=Path, help="output file (single database only)")
    parser.add_argument("--skip-inference", action="store_true", help="declared relationships only")
    parser.add_argument("--sample-n", type=int, default=5)
    parser.add_argument("--min-overlap", type=float, default=infer.MIN_OVERLAP)
    parser.add_argument("--min-confidence", type=float, default=0.5)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if bool(args.db) == args.all:
        parser.error("give one or more --db, or --all (not both)")
    db_ids = db.list_databases() if args.all else list(dict.fromkeys(args.db))
    if not db_ids:
        parser.error("no databases found")
    if args.out and len(db_ids) > 1:
        parser.error("--out only makes sense with a single database")

    results = build_many(
        db_ids,
        out=args.out,
        skip_inference=args.skip_inference,
        sample_n=args.sample_n,
        min_overlap=args.min_overlap,
        min_confidence=args.min_confidence,
    )
    print(format_results(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())