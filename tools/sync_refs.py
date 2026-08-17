"""Sync reference sources (HGNC, ClinVar-mini, ...) into refs.db from local
TSV/dump files. See docs/architecture-v2.md §4 and app/refs/.

Not required by any P0.3 acceptance criterion — makes the entity/reference
layer actually usable outside tests. An operator points --src at a directory
of TSVs and runs this once; idempotent and safe to re-run (bump a
ReferenceSource's ref_version to force reprocessing).

Usage:
  python -m tools.sync_refs --src /path/to/refs-dir [--db /data/refs.db]
"""
import argparse
from pathlib import Path

from app.refs import connect
from app.refs import store as refs_store


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", required=True, type=Path,
                    help="directory containing hgnc.tsv, clinvar_mini.tsv, ...")
    p.add_argument("--db", type=Path, default=None,
                    help="refs.db path (default: config.REFS_DB_PATH)")
    args = p.parse_args()

    con = connect(args.db)
    results = refs_store.sync_all(con, args.src)
    for source_id, result in results.items():
        print(f"{source_id}: {result}")


if __name__ == "__main__":
    main()
