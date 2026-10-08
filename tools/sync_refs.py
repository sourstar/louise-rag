"""Sync reference sources (HGNC, ClinVar-mini, ...) into refs.db.

With --fetch, the reference dumps download themselves first (HGNC complete
set, ClinVar variant_summary — HTTPS, budgeted and Range-resumable, see
app/refs/acquire.py); without it, --src must already contain the TSVs.
Idempotent and safe to re-run: downloads are freshness-gated by file age,
and syncing short-circuits unless a source's ref_version changed (bump it
to force reprocessing).

Usage:
  python -m app.louise dev refs-sync --src /path/to/refs-dir [--db /data/refs.db]
                             [--fetch] [--force]
"""
import argparse
from pathlib import Path

from app.refs import connect
from app.refs import acquire as refs_acquire
from app.refs import store as refs_store


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", required=True, type=Path,
                    help="directory containing (or receiving) hgnc.tsv, clinvar_mini.tsv, ...")
    p.add_argument("--db", type=Path, default=None,
                    help="refs.db path (default: config.REFS_DB_PATH)")
    p.add_argument("--fetch", action="store_true",
                    help="download/refresh the reference dumps into --src first")
    p.add_argument("--force", action="store_true",
                    help="with --fetch: re-download even if the local copy is fresh")
    args = p.parse_args()

    if args.fetch:
        for source_id, result in refs_acquire.fetch_all(
                args.src, force=args.force).items():
            print(f"fetch {source_id}: {result}")

    con = connect(args.db)
    results = refs_store.sync_all(con, args.src)
    for source_id, result in results.items():
        print(f"{source_id}: {result}")


if __name__ == "__main__":
    main()
