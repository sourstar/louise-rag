"""The cross-vendor deviation reference (docs/notes/approved-encoders.md,
operator 2026-10-21), measured on a machine that holds no collection: a rented
GPU, a card before its first peer.

    # on a node that holds the collection: its Codex and calibration member
    python -m tools.cross_vendor_check export gutenberg /tmp/gutenberg-space

    # on the machine under test, the folder copied over (07 §7.8's VEGA_URL set):
    python -m tools.cross_vendor_check measure /tmp/gutenberg-space \\
        --url http://localhost:30286 --api vega \\
        --model vega-qwen3-embedding-4b [--query-model …] \\
        [--twin vega-qwen3-embedding-4b-cpu] [--twin-texts 64] [--twin-limit-s 1800] \\
        [--out result.json]

What it prints is the table's row, per role: the whole calibration set (all
512 texts) against the space's reference, worst, median and how many below the
0.995 review line; and, with `--twin`, the card against the same model on its
own CPU for the first `--twin-texts` texts spread over the set, within
`--twin-limit-s` (a CPU embeds a 4B model at ~13 s a text: what it has at the
limit is reported, and how many).

NOTHING HERE IS A COPY OF THE GATE. The card's vectors come from the gate
itself (`hold.embedder.run_gate`, through `run_check.answers`, the node's
client): the texts it sends — the query side's instruction once, the pair form
of a pair space — are the texts the twin is sent, captured, not rebuilt. The
member is read through `calibration.read_member` / `read_query_vectors`, which
refuse one whose vectors are not the Codex's. Bit-for-bit repeatability over
fresh loads is tools/vega_repro_check.py's.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import collections as collections_mod, run_check  # noqa: E402
from app.hold import calibration as cal, codex as codex_mod, embedder as emb  # noqa: E402
from app.hold import collection as col  # noqa: E402

REVIEW_LINE = 0.995
CODEX = "codex.json"


def export(root: Path, ref: str, out: Path) -> list[Path]:
    """The space a measurement needs: the collection's Codex, byte for byte,
    and its calibration member. Nothing else leaves the node."""
    name, codex = col.resolve(root, ref)
    member = run_check._member(root, name, codex)
    if not member.is_file():
        raise SystemExit(f"{name}: no calibration member at {member}")
    out.mkdir(parents=True, exist_ok=True)
    written = [out / CODEX, out / member.name]
    shutil.copyfile(col.shard_dir_of(root, name) / col.CODEX_FILE, written[0])
    shutil.copyfile(member, written[1])
    return written


def load(space_dir: Path):
    """(codex, member path) from an exported folder."""
    codex = codex_mod.loads((space_dir / CODEX).read_bytes())
    members = sorted(space_dir.glob("*.safetensors"))
    if len(members) != 1:
        raise SystemExit(f"{space_dir}: expected one calibration member, found {len(members)}")
    return codex, members[0]


def reference(codex, member: Path, role: str) -> list[list[float]]:
    if role == "query":
        return cal.read_query_vectors(member, expect=codex.space.calibration.query_vectors).tolist()
    return cal.read_member(member, expect_vectors=codex.space.calibration.vectors,
                           expect_envelope=codex.space.envelope)[0].tolist()


def stats(cosines: list[float]) -> dict[str, Any]:
    return {"n": len(cosines), "worst": min(cosines), "median": statistics.median(cosines),
            "below": sum(1 for c in cosines if c < REVIEW_LINE)}


def spread(n: int, total: int) -> list[int]:
    """`n` indices at evenly spaced places in the set, all of them if fewer."""
    if n >= total:
        return list(range(total))
    return [i * total // n for i in range(n)]


def measure(space_dir: Path, *, url: str, api: str, model: str, query_model: str | None = None,
            twin: str | None = None, twin_texts: int = 64, twin_limit_s: float = 1800.0,
            say: Callable[[str], None] = print) -> dict[str, Any]:
    codex, member = load(space_dir)
    roles = ["document"] + (["query"] if codex.space.query.encoder is not None else [])
    recipe = collections_mod._codex_encoder(
        {"url": url, "api": api, "embed_model": model, "query_model": query_model or model},
        codex, "cross-vendor check")
    items = cal.items(codex, member)
    out: dict[str, Any] = {"collection_id": codex.collection_id, "space_id": codex.space_id,
                           "url": url, "model": model, "query_model": query_model or model,
                           "roles": {}}
    for role in roles:
        query = role == "query"
        ref = reference(codex, member, role)
        sent: list = []
        got: list = []
        embed = run_check.answers(recipe, query=query)

        def capturing(texts, _embed=embed):
            vectors = _embed(texts)
            sent.extend(texts)
            got.extend(vectors)
            return vectors

        run = emb.run_gate(codex, reference=ref, embed=capturing, role=role, items=items)
        whole = stats(list(run.all_cosines))
        entry: dict[str, Any] = {"whole_set": whole, "runs": sorted(str(r) for r in embed.runs),
                                 "cosines": list(run.all_cosines)}
        say(f"{role}: whole set vs reference: worst {whole['worst']:.6f}, median "
            f"{whole['median']:.6f}, below {REVIEW_LINE}: {whole['below']} of {whole['n']} "
            f"(run {', '.join(r[:23] + '…' for r in entry['runs']) or 'none stated'})")
        if twin:
            # the twin answers on the side asked, under its own model name
            twin_embed = run_check.answers(replace(recipe, model=twin, query_model=twin), query=query)
            picked = spread(twin_texts, len(sent))
            deadline = time.monotonic() + twin_limit_s
            pairs: list[float] = []
            for start in range(0, len(picked), 8):
                if time.monotonic() > deadline:
                    say(f"{role}: the twin stopped at its limit after {len(pairs)} of {len(picked)} texts")
                    break
                chunk = picked[start:start + 8]
                vectors = twin_embed([sent[i] for i in chunk])
                pairs += [emb.cosine(got[i], v) for i, v in zip(chunk, vectors)]
            twin_stats = stats(pairs) if pairs else {"n": 0}
            entry["twin"] = {"model": twin, "runs": sorted(str(r) for r in twin_embed.runs),
                             **twin_stats, "cosines": pairs}
            if pairs:
                say(f"{role}: card vs its CPU twin {twin}: worst {twin_stats['worst']:.6f}, "
                    f"median {twin_stats['median']:.6f}, below {REVIEW_LINE}: "
                    f"{twin_stats['below']} of {twin_stats['n']}")
        out["roles"][role] = entry
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tools.cross_vendor_check")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="a held collection's Codex and calibration member")
    e.add_argument("collection")
    e.add_argument("out", type=Path)
    e.add_argument("--root", type=Path, default=None)
    m = sub.add_parser("measure", help="the deviation row of the encoder at --url")
    m.add_argument("space", type=Path)
    m.add_argument("--url", required=True)
    m.add_argument("--api", default="vega")
    m.add_argument("--model", required=True)
    m.add_argument("--query-model")
    m.add_argument("--twin")
    m.add_argument("--twin-texts", type=int, default=64)
    m.add_argument("--twin-limit-s", type=float, default=1800.0)
    m.add_argument("--out", type=Path)
    a = p.parse_args(argv)
    if a.cmd == "export":
        root = a.root or (collections_mod.louise_root() / "collections")
        for path in export(root, a.collection, a.out):
            print(path)
        return 0
    result = measure(a.space, url=a.url, api=a.api, model=a.model, query_model=a.query_model,
                     twin=a.twin, twin_texts=a.twin_texts, twin_limit_s=a.twin_limit_s)
    if a.out:
        a.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
