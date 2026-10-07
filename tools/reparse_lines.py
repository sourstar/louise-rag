"""tools/reparse_lines.py — one image's re-parse of a seeded sample of a
collection's records, line by line, for 03 §3.4's `image-equivalence`
(amended 2026-10-07).

Run it under the Codex's own reference image to write the reference; then
`louise hold check <collection> --checks image-equivalence,chunks --origin
DIR --against FILE` under the image to be proven re-parses the same records
from the same origin bytes and compares. Only what the parser makes of the
bytes is compared — the operator's rule: "the source compliancy is after
the parsing of the useless information, that only matters".

It uses nothing the September images lack (`louise._reparse_from`,
`collection.resolve`, `check.sampled_shards`, `snapshot.load`,
`records.iter_records`), so it runs mounted into an old image:

  docker run --rm --network none -w /srv -e PYTHONPATH=/srv -v <data>:/data -v <origin>:/origin:ro -v <this file>:/tmp/r.py:ro \\
    -e LOUISE_PLATFORM_IMAGE=<the image's own config digest> louise-rag:codex-<name> \\
    python /tmp/r.py <collection> --origin /origin --seed <hex> --out /data/<file>.json

LOUISE_PLATFORM_IMAGE must be the image it runs in: `--against` refuses a
reference made by any image but the one the Codex names.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="reparse_lines")
    p.add_argument("collection")
    p.add_argument("--origin", required=True, help="the origin base the records' locators resolve in")
    p.add_argument("--seed", required=True, help="32 bytes as hex: the sample's seed")
    p.add_argument("--sample", default=None, help="a shard fraction overriding the plan's")
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)

    from app import louise
    from app.hold import check, collection as col, snapshot as snap
    from app.pack import records as pack_records

    image = (os.environ.get("LOUISE_PLATFORM_IMAGE") or "").strip()
    if not image.startswith("sha256:"):
        print("reparse_lines: LOUISE_PLATFORM_IMAGE must name the image this runs in", file=sys.stderr)
        return 2
    root = Path(os.environ.get("LOUISE_ROOT") or os.environ.get("DATA_DIR") or "/data") / "collections"
    name, codex = col.resolve(root, a.collection)
    drag = root / name / "drag"
    snaps = sorted(q for q in (drag / "snapshots").glob("*.json") if not q.name.endswith(".sig.json"))
    if not snaps:
        print(f"reparse_lines: {name} holds no snapshot", file=sys.stderr)
        return 2
    snapshot = snap.load(snaps[-1])
    seed = bytes.fromhex(a.seed)
    shards = check.sampled_shards(codex, snapshot, "records-vs-origin", seed=seed, override=a.sample)
    reparse = louise._reparse_from(codex, name, a.origin)
    lines: dict[str, list[str]] = {}
    errors: dict[str, str] = {}
    for s in shards:
        shard = drag / "records" / s.slice / f"{s.n:05d}.jsonl.zst"
        for record in pack_records.iter_records(shard):
            uid, origin = record.get("uid"), record.get("origin")
            if not uid or not origin:
                continue
            try:
                line = reparse(uid, origin)
            except Exception as e:  # noqa: BLE001 — named, never folded into the lines
                errors[uid] = f"{type(e).__name__}: {e}"[:200]
                continue
            lines[uid] = [origin, hashlib.sha256(line).hexdigest()]
    out = {"collection_id": codex.collection_id, "snapshot_hash": snapshot.hash, "image": image,
           "seed": a.seed, "shards": [f"{s.slice}/{s.n:05d}" for s in shards],
           "lines": lines, "errors": errors}
    Path(a.out).write_text(json.dumps(out, sort_keys=True), encoding="utf-8")
    print(f"reparse_lines: {len(lines)} line(s) over {len(shards)} shard(s), {len(errors)} error(s), "
          f"image {image[:19]}… -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
