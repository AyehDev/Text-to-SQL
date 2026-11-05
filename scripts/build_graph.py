#!/usr/bin/env python3
"""Build the graph for a database: extract, infer, ingest, weight, embed, verify.

    python scripts/build_graph.py --db adventureworks [--rebuild] [--skip-inference] [--skip-enrichment]

The work is in schemagraph/graph/build.py; this file only lets you run it as a script
from a checkout without installing the package.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemagraph.graph.build import main

if __name__ == "__main__":
    raise SystemExit(main())
