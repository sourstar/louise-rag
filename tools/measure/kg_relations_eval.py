"""Which LLM extracts wiki-gutenberg's relations best — measured on the
published set whose triples are our claim's shape (operator, 2026-11-03:
"Evaluate using a Nemotron model for the task"; wiki-gutenberg-amendments.md,
"Nemotron for the task").

    python -m tools.measure.kg_relations_eval fetch --out DIR
    python -m tools.measure.kg_relations_eval run   --out DIR --name gemma-4-26b \\
        --url http://nas:30437 --model gemma-4-26b-a4b [--limit 100] [--concurrency 2] \\
        [--extra '{"chat_template_kwargs": {"enable_thinking": false}}']
    python -m tools.measure.kg_relations_eval score --out DIR [--baseline gemma-4-26b]

THE SET, NOT OURS ("do not reinvent the wheel"). Re-DocRED (Tan et al., EMNLP
2022; MIT): DocRED's 500 test documents re-annotated for its false negatives.
- Each document gives its entities (all mentions of each) and the gold
  triples (head entity, Wikidata property, tail entity) over 96 properties.
- That is wiki-gutenberg's `relation` claim exactly: a Q, a property, a Q,
  read from one passage.
- The entities are GIVEN, so this scores the relation step alone, as the
  pipeline runs it (mentions and links come first).

THE PROMPT is one text for every model, built from the document alone. Its
sha256 is recorded with the run, so two rows that used different prompts
cannot be compared by mistake. The property names come from Wikidata itself
(`wbgetentities`, English labels), fetched once beside the data.

THE ENGINE'S RULES (invariant 12, and the agreement floor decided
2026-10-20): temperature 0 and a fixed seed. `--extra` passes what a model
needs to answer without thinking (e.g. Nemotron's and Gemma's
`chat_template_kwargs.enable_thinking=false`). It is recorded with the run.

THE SCORE is Re-DocRED's own: micro precision, recall and F1 over all gold
triples. DocRED's "Ign F1", which leaves out facts seen in training, does not
apply: no model here was trained on Re-DocRED's train split by us. Per
document F1 feeds the paired randomization test of tools/encoder_bakeoff.py
(Smucker et al., CIKM 2007) between a run and `--baseline`.

Resumable: answers append one document at a time; a run with other settings
under the same name is refused.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

DATA_URL = "https://huggingface.co/datasets/tonytan48/Re-DocRED/resolve/main/{name}"
SPLITS = ("test_revised.json", "dev_revised.json")
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
LICENCE = ("Re-DocRED (Tan, Xu, Bing, Ng, Aljunied; EMNLP 2022), "
           "https://huggingface.co/datasets/tonytan48/Re-DocRED, MIT licence.\n")
MAX_TOKENS = 2048

SYSTEM = ("You extract relations between named entities from a text. "
          "Answer with JSON only.")


@dataclass(frozen=True)
class Doc:
    index: int
    title: str
    text: str
    entities: list[tuple[str, str, tuple[str, ...]]]   # (name, type, other forms) by number
    gold: frozenset[tuple[int, str, int]]    # (head, property, tail)


def load_docs(path: Path) -> list[Doc]:
    """DocRED's layout: `sents` (token lists), `vertexSet` (an entity's
    mentions, each with `name` and `type`), `labels` ({h, t, r})."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    docs = []
    for i, d in enumerate(raw):
        text = " ".join(" ".join(s) for s in d["sents"])
        entities = [_entity(v) for v in d["vertexSet"]]
        gold = frozenset((int(l["h"]), str(l["r"]), int(l["t"])) for l in d.get("labels", []))
        docs.append(Doc(i, d.get("title", ""), text, entities, gold))
    return docs


def _entity(mentions: list[dict]) -> tuple[str, str, tuple[str, ...]]:
    """An entity's name is its most frequent mention (the longest on a tie):
    the first is not always the best ("Loud" for the Loud Tour). The other
    forms are listed, so the model can find every mention."""
    counts: dict[str, int] = {}
    for m in mentions:
        counts[m["name"]] = counts.get(m["name"], 0) + 1
    name = max(counts, key=lambda n: (counts[n], len(n), n))
    return name, mentions[0].get("type", ""), tuple(sorted(n for n in counts if n != name))


def properties_of(docs: Iterable[Doc]) -> list[str]:
    """The property ids the set uses, in Wikidata's numeric order."""
    ids = {r for d in docs for _, r, _ in d.gold}
    return sorted(ids, key=lambda p: int(p[1:]))


def prompt_for(doc: Doc, names: dict[str, str]) -> str:
    ents = "\n".join(f"{i}: {n}" + (f" (also: {', '.join(a)})" if a else "") + (f" [{t}]" if t else "")
                     for i, (n, t, a) in enumerate(doc.entities))
    props = "\n".join(f"{p} {names.get(p, p)}" for p in sorted(names, key=lambda p: int(p[1:])))
    return (f"Text:\n{doc.text}\n\n"
            f"Entities:\n{ents}\n\n"
            f"Relations (Wikidata properties):\n{props}\n\n"
            "List every relation that the text states or clearly implies between two of "
            "the listed entities, as JSON: "
            '{"relations": [{"head": <entity number>, "relation": "<property id>", '
            '"tail": <entity number>}]}. Use only the property ids above and only the '
            'entity numbers above. If there is none, answer {"relations": []}.')


def prompt_hash(names: dict[str, str]) -> str:
    """The prompt's fixed parts: the system text, the property list and the
    template, with a placeholder document — what makes two runs comparable."""
    probe = Doc(-1, "", "<text>", [("<entity>", "", ())], frozenset())
    blob = SYSTEM + "\x00" + prompt_for(probe, names)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_JSON = re.compile(r"\{.*\}", re.S)


def parse(answer: str, n_entities: int, allowed: set[str]) -> tuple[set[tuple[int, str, int]], bool]:
    """(the valid triples, whether the answer parsed at all). Code fences and
    text around the JSON are tolerated; a triple with an unknown property, an
    entity number out of range or a self-relation is dropped, never guessed."""
    m = _JSON.search(answer or "")
    if m is None:
        return set(), False
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return set(), False
    rows = obj.get("relations") if isinstance(obj, dict) else None
    if not isinstance(rows, list):
        return set(), False
    out: set[tuple[int, str, int]] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        h, rel, t = r.get("head"), r.get("relation"), r.get("tail")
        if isinstance(h, str) and h.isdigit():
            h = int(h)
        if isinstance(t, str) and t.isdigit():
            t = int(t)
        if not (isinstance(h, int) and isinstance(t, int) and isinstance(rel, str)):
            continue
        if rel not in allowed or h == t or not (0 <= h < n_entities and 0 <= t < n_entities):
            continue
        out.add((h, rel, t))
    return out, True


def prf(pred: set, gold: set) -> tuple[int, int, int]:
    """(true positives, predicted, gold)."""
    return len(pred & gold), len(pred), len(gold)


def f1(tp: int, n_pred: int, n_gold: int) -> tuple[float, float, float]:
    p = tp / n_pred if n_pred else 0.0
    r = tp / n_gold if n_gold else 0.0
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


# ---- the steps -----------------------------------------------------------------

def fetch(out: Path, *, download: Callable[[str, Path], None] | None = None,
          labels: Callable[[list[str]], dict[str, str]] | None = None) -> int:
    from tools.encoder_bakeoff import _download, _sha256
    download = download or _download
    out.mkdir(parents=True, exist_ok=True)
    (out / "LICENCE.txt").write_text(LICENCE, encoding="utf-8")
    for name in SPLITS:
        target = out / name
        if not target.exists():
            download(DATA_URL.format(name=name), target)
    docs = load_docs(out / SPLITS[0])
    props = properties_of(docs)
    names = (labels or wikidata_labels)(props)
    (out / "properties.json").write_text(json.dumps(names, indent=1, sort_keys=True), encoding="utf-8")
    sums = {name: _sha256(out / name) for name in SPLITS}
    (out / "SHA256SUMS.json").write_text(json.dumps(sums, indent=1), encoding="utf-8")
    print(f"{len(docs)} test documents, {sum(len(d.gold) for d in docs)} gold triples, "
          f"{len(props)} properties")
    return 0


def wikidata_labels(ids: list[str]) -> dict[str, str]:
    """English labels of Wikidata properties, 50 ids a request (the API's
    limit for anonymous clients)."""
    import httpx
    names: dict[str, str] = {}
    for i in range(0, len(ids), 50):
        part = ids[i:i + 50]
        r = httpx.get(WIKIDATA_API, params={"action": "wbgetentities", "ids": "|".join(part),
                                            "props": "labels", "languages": "en", "format": "json"},
                      headers={"User-Agent": "louise-kg-relations-eval/1"}, timeout=60)
        r.raise_for_status()
        for pid, ent in r.json().get("entities", {}).items():
            names[pid] = ent.get("labels", {}).get("en", {}).get("value", pid)
    return names


def chat_http(url: str, model: str, extra: dict[str, Any], timeout: float = 600.0
              ) -> Callable[[str], str]:
    import httpx
    client = httpx.Client(timeout=timeout)

    def ask(prompt: str) -> str:
        body = {"model": model, "temperature": 0, "seed": 0, "max_tokens": MAX_TOKENS,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": prompt}], **extra}
        r = client.post(url.rstrip("/") + "/v1/chat/completions", json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"].get("content") or ""
    return ask


def run(out: Path, name: str, *, model: str, url: str, extra: dict[str, Any] | None = None,
        limit: int | None = None, concurrency: int = 2,
        ask: Callable[[str], str] | None = None) -> int:
    docs = load_docs(out / SPLITS[0])[:limit] if limit else load_docs(out / SPLITS[0])
    names = json.loads((out / "properties.json").read_text(encoding="utf-8"))
    run_dir = out / "runs" / name
    run_dir.mkdir(parents=True, exist_ok=True)
    settings = {"model": model, "url": url, "extra": extra or {}, "temperature": 0, "seed": 0,
                "max_tokens": MAX_TOKENS, "prompt_sha256": prompt_hash(names)}
    stated = run_dir / "settings.json"
    if stated.exists():
        before = json.loads(stated.read_text(encoding="utf-8"))
        if before != settings:
            raise SystemExit(f"{name}: settings differ from this run's first ones "
                             f"({stated}); use another --name")
    else:
        stated.write_text(json.dumps(settings, indent=1, sort_keys=True), encoding="utf-8")
    answers = run_dir / "answers.jsonl"
    done = set()
    if answers.exists():
        done = {json.loads(l)["index"] for l in answers.read_text(encoding="utf-8").splitlines() if l}
    todo = [d for d in docs if d.index not in done]
    ask = ask or chat_http(url, model, extra or {})
    lock = threading.Lock()

    def one(doc: Doc) -> None:
        text = ask(prompt_for(doc, names))
        with lock, answers.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"index": doc.index, "answer": text}, ensure_ascii=False) + "\n")
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for f in [pool.submit(one, d) for d in todo]:
            f.result()
    print(f"{name}: {len(todo)} answered now, {len(done) + len(todo)} of {len(docs)}")
    return 0


def score_run(out: Path, name: str) -> dict[str, Any]:
    docs = {d.index: d for d in load_docs(out / SPLITS[0])}
    allowed = set(json.loads((out / "properties.json").read_text(encoding="utf-8")))
    rows = [json.loads(l) for l in (out / "runs" / name / "answers.jsonl")
            .read_text(encoding="utf-8").splitlines() if l]
    tp = n_pred = n_gold = unparsed = 0
    per_doc: dict[int, float] = {}
    for row in rows:
        doc = docs[row["index"]]
        pred, ok = parse(row["answer"], len(doc.entities), allowed)
        unparsed += not ok
        a, b, c = prf(pred, set(doc.gold))
        tp, n_pred, n_gold = tp + a, n_pred + b, n_gold + c
        per_doc[doc.index] = f1(a, b, c)[2]
    p, r, f = f1(tp, n_pred, n_gold)
    return {"name": name, "docs": len(rows), "precision": p, "recall": r, "f1": f,
            "unparsed": unparsed, "per_doc": per_doc}


def score(out: Path, baseline: str | None = None) -> int:
    from tools.encoder_bakeoff import paired
    names = sorted(p.name for p in (out / "runs").iterdir() if (p / "answers.jsonl").exists())
    results = {n: score_run(out, n) for n in names}
    print("| run | documents | precision | recall | F1 | unparsed | Δ F1 vs baseline [95 %] | p |")
    print("|---|---|---|---|---|---|---|---|")
    for n, s in results.items():
        cmp_cell, p_cell = "—", "—"
        if baseline and n != baseline and baseline in results:
            common = sorted(set(s["per_doc"]) & set(results[baseline]["per_doc"]))
            d, lo, hi, pv = paired([s["per_doc"][i] for i in common],
                                   [results[baseline]["per_doc"][i] for i in common])
            cmp_cell, p_cell = f"{d:+.3f} [{lo:+.3f}, {hi:+.3f}] over {len(common)}", f"{pv:.4f}"
        print(f"| {n} | {s['docs']} | {s['precision']:.3f} | {s['recall']:.3f} | "
              f"**{s['f1']:.3f}** | {s['unparsed']} | {cmp_cell} | {p_cell} |")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch"); p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("run")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--extra", default="{}", help="JSON merged into each request body")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=2)
    p = sub.add_parser("score")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--baseline", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        return fetch(a.out)
    if a.cmd == "run":
        return run(a.out, a.name, model=a.model, url=a.url, extra=json.loads(a.extra),
                   limit=a.limit, concurrency=a.concurrency)
    return score(a.out, a.baseline)


if __name__ == "__main__":
    sys.exit(main())
