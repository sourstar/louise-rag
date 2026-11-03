"""Which Nemotron picks a mention's Wikidata entity best among its top 10 —
wiki-gutenberg's linking step, measured on a published set (operator,
2026-11-03: "compare the 2 nemo options during the night when no one is
using louise"; wiki-gutenberg-amendments.md, "Nemotron for the task").

    python -m tools.measure.kg_link_pick_eval fetch      --out DIR [--langs en,de,es]
    python -m tools.measure.kg_link_pick_eval candidates --out DIR
    python -m tools.measure.kg_link_pick_eval pick --out DIR --name rerank-8b --kind rerank \\
        --url http://nas:30437 --model nemotron-3.5-rerank-8b [--limit 300] [--concurrency 2]
    python -m tools.measure.kg_link_pick_eval pick --out DIR --name lightning-30b --kind llm \\
        --url http://nas:30437 --model nemotron-3.5-lightning-30b-a3b \\
        [--extra '{"chat_template_kwargs": {"enable_thinking": false}}']
    python -m tools.measure.kg_link_pick_eval score --out DIR [--baseline rerank-8b]

THE SET, NOT OURS. Mewsli-X (XTREME-R, Ruder et al., EMNLP 2021; Apache-2.0,
`izhx/mewsli-x`):
- WikiNews mentions in 11 languages, each linked to a Wikidata QID; the
  mention is marked `{ … }` in its sentence.
- ~1 M candidate entities, each with a description.

THE STEP ALONE. In the pipeline the top 10 come from wikidata's Nemotron
vectors, which are not all there yet. Here they come from BM25 over the
set's own candidate descriptions, the same for every picker, so the pickers
are compared on the same lists. Two numbers are reported:
- the word search's recall@10, which is the first stage's, not the picker's;
- each picker's accuracy@1 over the mentions whose gold is in the 10. That
  is the picker's own number, and the paired test runs on it.

THE PICKERS:
- `rerank`: a cross-encoder through LocalAI's /v1/rerank (Jina's API). The
  query is the mention in its sentence, the documents are the 10
  descriptions, and the highest score wins.
- `llm`: a chat model through /v1/chat/completions, temperature 0, seed 0.
  It sees the sentence and the numbered descriptions and answers
  {"choice": n}; an answer that does not parse counts as wrong.
The prompt's sha256 is recorded with each run, and a run with other settings
under the same name is refused. Resumable, one mention at a time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

BASE = "https://huggingface.co/datasets/izhx/mewsli-x/resolve/main/"
CANDIDATES_TAR = "candidate_entities.jsonl.tar.gz"
CANDIDATES = "candidate_entities.jsonl"
LICENCE = ("Mewsli-X (Botha, Shan, Gillick; Ruder et al., XTREME-R, EMNLP 2021), "
           "https://huggingface.co/datasets/izhx/mewsli-x, Apache-2.0.\n")
TOP = 10
MAX_DESC = 600
SYSTEM = "You link a mention in a sentence to the entity it refers to. Answer with JSON only."


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def surface_and_context(mention: str) -> tuple[str, str]:
    """Mewsli-X writes "<surface>: <sentence with { surface }>"."""
    head, _, rest = mention.partition(": ")
    return head, rest or mention


def mentions(out: Path) -> list[dict[str, Any]]:
    rows = []
    for lang_file in sorted((out / "mentions").glob("*.jsonl")):
        for r in _jsonl(lang_file):
            rows.append({**r, "lang": lang_file.stem})
    return rows


# ---- fetch and candidates ------------------------------------------------------

def fetch(out: Path, langs: list[str], *, download: Callable[[str, Path], None] | None = None) -> int:
    from tools.encoder_bakeoff import _download, _sha256
    download = download or _download
    (out / "mentions").mkdir(parents=True, exist_ok=True)
    (out / "LICENCE.txt").write_text(LICENCE, encoding="utf-8")
    for lang in langs:
        target = out / "mentions" / f"{lang}.jsonl"
        if not target.exists():
            download(BASE + f"wikinews_mentions/{lang}/test.jsonl", target)
    if not (out / CANDIDATES).exists():
        tar = out / CANDIDATES_TAR
        if not tar.exists():
            download(BASE + CANDIDATES_TAR, tar)
        with tarfile.open(tar) as t:
            member = next(m for m in t.getmembers() if m.name.endswith(CANDIDATES))
            src = t.extractfile(member)
            assert src is not None
            tmp = out / (CANDIDATES + ".part")
            with tmp.open("wb") as fh:
                while block := src.read(1 << 20):
                    fh.write(block)
            tmp.replace(out / CANDIDATES)
    sums = {p.name: _sha256(p) for p in [out / CANDIDATES, *sorted((out / "mentions").glob("*.jsonl"))]}
    (out / "SHA256SUMS.json").write_text(json.dumps(sums, indent=1), encoding="utf-8")
    print(f"{len(mentions(out))} mentions in {', '.join(langs)}")
    return 0


def candidates(out: Path, *, top: int = TOP) -> int:
    """BM25 (tantivy, the index the app's word search uses) over the
    candidate descriptions; the surface form is the query."""
    import tantivy
    index_dir = out / "bm25"
    b = tantivy.SchemaBuilder()
    b.add_text_field("entity_id", stored=True, tokenizer_name="raw")
    b.add_text_field("description", stored=False, tokenizer_name="default")
    schema = b.build()
    fresh = not index_dir.exists()
    index_dir.mkdir(exist_ok=True)
    idx = tantivy.Index(schema, path=str(index_dir))
    if fresh:
        w = idx.writer(heap_size=256_000_000)
        with (out / CANDIDATES).open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    e = json.loads(line)
                    w.add_document(tantivy.Document(entity_id=e["entity_id"],
                                                    description=e["description"]))
        w.commit()
        w.wait_merging_threads()
    idx.reload()
    searcher = idx.searcher()
    found, hits = [], 0
    for m in mentions(out):
        surface, _ = surface_and_context(m["mention"])
        terms = re.sub(r"[^\w\s]", " ", surface).strip()
        ids: list[str] = []
        if terms:
            q = idx.parse_query(terms, ["description"])
            ids = [searcher.doc(addr)["entity_id"][0] for _, addr in searcher.search(q, top).hits]
        hits += m["entity_id"] in ids
        found.append({"example_id": m["example_id"], "candidates": ids})
    (out / "candidates.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in found), encoding="utf-8")
    print(f"recall@{top} of the word search: {hits / max(1, len(found)):.3f} over {len(found)}")
    return 0


def descriptions(out: Path, wanted: set[str]) -> dict[str, str]:
    got: dict[str, str] = {}
    with (out / CANDIDATES).open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            e = json.loads(line)
            if e["entity_id"] in wanted:
                got[e["entity_id"]] = e["description"][:MAX_DESC]
    return got


# ---- the pickers ---------------------------------------------------------------

def llm_prompt(mention: str, docs: list[str]) -> str:
    surface, context = surface_and_context(mention)
    numbered = "\n".join(f"{i + 1}. {d}" for i, d in enumerate(docs))
    return (f"Sentence: {context}\n"
            f"The mention is the text in braces: {surface}\n\n"
            f"Candidate entities:\n{numbered}\n\n"
            "Which candidate is the entity the mention refers to? Answer as JSON: "
            '{"choice": <candidate number>}, or {"choice": null} if none of them.')


def prompt_hash(kind: str) -> str:
    probe = (SYSTEM + "\x00" + llm_prompt("<s>: <sentence { <s> }>", ["<description>"])
             if kind == "llm" else "rerank:<mention in its sentence>|<description>")
    return hashlib.sha256(probe.encode("utf-8")).hexdigest()


_JSON = re.compile(r"\{.*\}", re.DOTALL)


def parse_choice(answer: str, n: int) -> int | None:
    """The 0-based index chosen, or None: no JSON, null, or out of range."""
    m = _JSON.search(answer or "")
    if m is None:
        return None
    try:
        c = json.loads(m.group(0)).get("choice")
    except (json.JSONDecodeError, AttributeError):
        return None
    if isinstance(c, str) and c.isdigit():
        c = int(c)
    return c - 1 if isinstance(c, int) and 1 <= c <= n else None


def llm_picker(url: str, model: str, extra: dict[str, Any]) -> Callable[[str, list[str]], int | None]:
    import httpx
    client = httpx.Client(timeout=600)

    def pick(mention: str, docs: list[str]) -> int | None:
        body = {"model": model, "temperature": 0, "seed": 0, "max_tokens": 64,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": llm_prompt(mention, docs)}], **extra}
        r = client.post(url.rstrip("/") + "/v1/chat/completions", json=body)
        r.raise_for_status()
        return parse_choice(r.json()["choices"][0]["message"].get("content") or "", len(docs))
    return pick


def rerank_picker(url: str, model: str, extra: dict[str, Any]) -> Callable[[str, list[str]], int | None]:
    import httpx
    client = httpx.Client(timeout=600)

    def pick(mention: str, docs: list[str]) -> int | None:
        surface, context = surface_and_context(mention)
        body = {"model": model, "query": f"{surface} | {context}", "documents": docs,
                "top_n": len(docs), **extra}
        r = client.post(url.rstrip("/") + "/v1/rerank", json=body)
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            return None
        return int(max(results, key=lambda x: x["relevance_score"])["index"])
    return pick


def pick(out: Path, name: str, *, kind: str, model: str, url: str,
         extra: dict[str, Any] | None = None, limit: int | None = None, concurrency: int = 2,
         picker: Callable[[str, list[str]], int | None] | None = None) -> int:
    rows = mentions(out)
    cands = {r["example_id"]: r["candidates"] for r in _jsonl(out / "candidates.jsonl")}
    # the picker's own number counts the mentions whose gold is in the 10:
    # ask only those, a deterministic prefix when --limit cuts
    todo_all = [m for m in rows if m["entity_id"] in cands.get(m["example_id"], [])]
    if limit:
        todo_all = todo_all[:limit]
    run_dir = out / "runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    settings = {"kind": kind, "model": model, "url": url, "extra": extra or {},
                "prompt_sha256": prompt_hash(kind), "top": TOP, "max_desc": MAX_DESC}
    stated = run_dir / "settings.json"
    if stated.exists():
        if json.loads(stated.read_text(encoding="utf-8")) != settings:
            raise SystemExit(f"{name}: settings differ from this run's first ones ({stated})")
    else:
        stated.write_text(json.dumps(settings, indent=1, sort_keys=True), encoding="utf-8")
    answers = run_dir / "answers.jsonl"
    done = {r["example_id"] for r in _jsonl(answers)} if answers.exists() else set()
    todo = [m for m in todo_all if m["example_id"] not in done]
    desc = descriptions(out, {e for m in todo for e in cands[m["example_id"]]})
    picker = picker or (rerank_picker if kind == "rerank" else llm_picker)(url, model, extra or {})
    lock = threading.Lock()

    def one(m: dict[str, Any]) -> None:
        ids = cands[m["example_id"]]
        chosen = picker(m["mention"], [desc.get(e, e) for e in ids])
        row = {"example_id": m["example_id"], "lang": m["lang"],
               "chosen": ids[chosen] if chosen is not None else None,
               "correct": chosen is not None and ids[chosen] == m["entity_id"]}
        with lock, answers.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for f in [pool.submit(one, m) for m in todo]:
            f.result()
    print(f"{name}: {len(todo)} picked now, {len(done) + len(todo)} of {len(todo_all)}")
    return 0


def score(out: Path, baseline: str | None = None) -> int:
    from tools.encoder_bakeoff import paired
    rows = mentions(out)
    cands = {r["example_id"]: r["candidates"] for r in _jsonl(out / "candidates.jsonl")}
    in_top = [m for m in rows if m["entity_id"] in cands.get(m["example_id"], [])]
    print(f"word search recall@{TOP}: {len(in_top) / max(1, len(rows)):.3f} ({len(in_top)} of {len(rows)})")
    runs = {p.name: {r["example_id"]: float(r["correct"]) for r in _jsonl(p / "answers.jsonl")}
            for p in sorted((out / "runs").iterdir()) if (p / "answers.jsonl").exists()}
    print("| picker | mentions | accuracy@1 (gold in the 10) | Δ vs baseline [95 %] | p |")
    print("|---|---|---|---|---|")
    for name, got in runs.items():
        cell, pv = "—", "—"
        if baseline and name != baseline and baseline in runs:
            common = sorted(set(got) & set(runs[baseline]))
            d, lo, hi, p = paired([got[i] for i in common], [runs[baseline][i] for i in common])
            cell, pv = f"{d:+.3f} [{lo:+.3f}, {hi:+.3f}] over {len(common)}", f"{p:.4f}"
        acc = sum(got.values()) / max(1, len(got))
        print(f"| {name} | {len(got)} | **{acc:.3f}** | {cell} | {pv} |")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--langs", default="en,de,es")
    p = sub.add_parser("candidates")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("pick")
    for a in ("--out", "--name", "--url", "--model"):
        p.add_argument(a, required=True, type=Path if a == "--out" else str)
    p.add_argument("--kind", choices=("rerank", "llm"), required=True)
    p.add_argument("--extra", default="{}")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=2)
    p = sub.add_parser("score")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--baseline", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        return fetch(a.out, [x for x in a.langs.split(",") if x])
    if a.cmd == "candidates":
        return candidates(a.out)
    if a.cmd == "pick":
        return pick(a.out, a.name, kind=a.kind, model=a.model, url=a.url,
                    extra=json.loads(a.extra), limit=a.limit, concurrency=a.concurrency)
    return score(a.out, a.baseline)


if __name__ == "__main__":
    sys.exit(main())
