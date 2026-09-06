"""Convert an origin corpus into a PACK (docs/notes/pack-format.md) — the
conversion script for gutenberg, pubmed, wikipedia (and the pre-embedded
`embedded` corpus), usable WITHOUT a configured collection:

    python -m tools.pack_convert --plugin pubmed --corpus /corpora/pubmed --out /packs/pubmed
    python -m tools.pack_convert --plugin gutenberg --corpus /corpora/gutenberg --out /packs/gutenberg
    python -m tools.pack_convert --plugin wikipedia --corpus /corpora/wikipedia --out /packs/wikipedia

It discovers the corpus into a build-local manifest (`<out>/.build/`), so
the plugin's own dedup/tombstone logic applies (a revised PubMed record,
a DeleteCitation), then runs `pack build`. Resumable: re-run to continue.
An existing collection can be packed instead with
`python -m app.cli pack build <collection> --out DIR`.

Only the useful data is packed: parsed text and metadata. Nothing else
from the corpus (epubs, XML markup, dump indexes) enters the pack.
"""
import argparse
import signal
import sys
import threading
import time
from pathlib import Path

from app import collections as collections_mod, db, discovery
from app.pack import build as pack_build

_stop = threading.Event()


def _sigterm(signum, frame):
    _stop.set()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plugin", required=True, choices=sorted(collections_mod.PLUGIN_REGISTRY))
    ap.add_argument("--corpus", required=True, help="the origin corpus directory")
    ap.add_argument("--out", required=True, help="the pack directory to create (a new dataset)")
    ap.add_argument("--name", help="collection name in the manifest (default: the plugin)")
    ap.add_argument("--snapshot", help="snapshot date (default: today)")
    ap.add_argument("--records-per-shard", type=int, help="override the plugin's shard size")
    ap.add_argument("--until", help="a CLOSED snapshot: records that existed at the origin on this "
                                    "date (YYYY-MM-DD) and nothing later")
    ap.add_argument("--unsigned", action="store_true", help="do not sign the manifest")
    ap.add_argument("--skip-discovery", action="store_true",
                    help="reuse the build manifest from a previous run without rescanning")
    args = ap.parse_args(argv)

    out = Path(args.out)
    state = out / pack_build.BUILD_DIR
    state.mkdir(parents=True, exist_ok=True)
    collection = collections_mod.Collection(
        name=args.name or args.plugin, plugin=args.plugin, profile=None, embed_mode_default=None,
        corpus_dir=Path(args.corpus), manifest_path=state / "manifest.db",
        fts_dir=state / "fts", lance_dir=state / "lancedb")
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    def log(msg: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)

    if not args.skip_discovery:
        con = db.connect(collection)
        log(f"[pack convert] discovering {args.plugin} corpus at {args.corpus}")
        stats = discovery.sync_collection(con, collection, stop=_stop)
        con.close()
        log(f"[pack convert] discovery: {stats}")
        if stats.get("interrupted"):
            return 130
    result = pack_build.build(collection, out, snapshot=args.snapshot, name=args.name,
                              records_per_shard=args.records_per_shard, until=args.until,
                              sign=not args.unsigned, log=log, stop=_stop)
    if result.interrupted:
        log("[pack convert] interrupted — re-run to resume")
        return 130
    log(f"[pack convert] done: {result.slices} slice(s), {result.shards} shard(s), "
        f"{result.records} records, {result.chunks} chunks, {result.skipped} skipped -> {result.manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
