"""tools/build_pair_calibration_set.py — freeze a PAIR space's calibration set.

    python tools/build_pair_calibration_set.py <louise-root> <collection> \
        [--snapshot ID] [--shards 16] --out pairs.jsonl [--provenance pairs.sources.json]

The pair counterpart of tools/build_calibration_set.py (09 §9.12, amended
2026-10-08; docs/notes/input-forms.md). A space whose layers feed the
encoder tokenizer text PAIRS is calibrated on 512 title-and-abstract pairs
drawn from the collection's own records, so that the gate measures the form
the pipeline writes: a plain-text gate would pass a backend that flattens
pairs, which is the defect the pair form exists to keep out.

Run once; the OUTPUT is the artefact, as for the text set. The rule is
`calibration.pair_set_from_drag`'s and depends on the snapshot alone, so
anyone holding it re-runs this and gets the same bytes; `--provenance`
records the snapshot, the shards and the uid of every pair.

Unlike the text set, the file is NOT shipped with the platform: those
records are the collection's, under its licence, and the set travels inside
the calibration member that `hold collection create --calibration-set FILE`
writes, fetched only after the acknowledgement (04 §4.4). The recipe names
the set by the sha256 this prints, as `space.calibration.set`; `hold
embedder calibrate --pairs FILE` measures the tolerance over it.

The records may be those of the collection that embeds text today (pubmed's
version-4 copy): each is cut in the PAIR form of its own layer, which is what
the pair collection's records give back byte for byte.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.hold import calibration as cal                 # noqa: E402
from app.hold import collection as col                   # noqa: E402
from app.hold import drag as drag_mod                    # noqa: E402
from app.hold import snapshot as snap                    # noqa: E402


def _snapshot(drag_dir: Path, snapshot_id: str | None):
    if snapshot_id:
        return snap.load(drag_dir / drag_mod.snapshot_path(snapshot_id))
    d = drag_dir / "snapshots"
    ids = sorted(p.stem for p in d.glob("*.json")
                 if not p.name.endswith(".sig.json")) if d.is_dir() else []
    if not ids:
        raise SystemExit(f"{drag_dir} holds no snapshot to draw from")
    return snap.load(d / f"{ids[-1]}.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", type=Path, help="<LOUISE_ROOT>/collections")
    ap.add_argument("collection", help="the collection whose records the pairs come from")
    ap.add_argument("--snapshot", default=None, help="a snapshot id (default: the latest)")
    ap.add_argument("--shards", type=int, default=cal.PAIR_SHARDS,
                    help="how many of the snapshot's shards to draw from")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--provenance", type=Path, default=None)
    a = ap.parse_args(argv)

    name, codex = col.resolve(a.root, a.collection)
    drag_dir = col.drag_of(a.root, name)
    snapshot = _snapshot(drag_dir, a.snapshot)
    data, provenance = cal.pair_set_from_drag(drag_dir, codex, snapshot, shards=a.shards)

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(data)
    if a.provenance:
        a.provenance.write_text(json.dumps(provenance, indent=1), encoding="utf-8")
    pairs = cal.pairs_of(data)
    lens = sorted(cal.item_bytes(p) for p in pairs)
    print(f"  {a.out}  {len(data):,} bytes, {len(pairs)} pairs")
    print(f"  set id  {cal.set_id(data)}   (space.calibration.set)")
    print(f"  bytes   min {lens[0]}  p50 {lens[len(lens) // 2]}  max {lens[-1]}")
    print(f"  from    {len(provenance['shards'])} shards of snapshot {snapshot.snapshot_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
