"""A llama.cpp conversion against the model as sentence-transformers runs it:
the CONVERSION CHECK a candidate encoder passes before any of its rows count
(gutenberg-encoder-bakeoff.md, "Before its row counts"; nemotron-embed.md).

    # 1. the texts, in the app image: from a wikidata_encoder_ab sample
    python -m tools.measure.st_parity pick OUT_DIR --docs 32 --queries 16 > texts.jsonl

    # 2. the reference, in a THROWAWAY venv with torch and sentence-transformers
    #    (never the app image: invariant 7); fp32 on the CPU, the weights' truth
    python tools/measure/st_parity.py reference --model /path/to/hf/snapshot \\
        --texts texts.jsonl --out ref.npy --query-prompt "query: " --document-prompt "passage: "

    # 3. the candidate, in the worker's env: the GGUF through VeGa and the
    #    production client, exactly as wikidata_encoder_ab embeds for it
    python -m tools.measure.st_parity compare --texts texts.jsonl --ref ref.npy --encoder nemotron

    # 0. FIRST, over EVERY text of the sets (no card, minutes): the engine's
    #    token ids against the model's own tokenizer.json, the exact strings
    #    the rows embed. Each text that differs joins steps 2 and 3
    python -m tools.measure.st_parity tokens --sample W1_DIR --sample P3_DIR \
        --tokenizer <snapshot>/tokenizer.json --encoder nemotron --out tok-texts.jsonl

WHY STEP 0 (2026-11-02). The 48 picked texts passed at 0.999999 on an engine
whose pre-tokenizer was not the model's: 71 of 40,931 texts (contractions,
Devanagari, Gujarati and Thai marks) tokenized differently, and 4 of them
then fell under 0.99. A sample finds what is common; a tokenizer fault is
rare by construction, so every text is tokenized, which costs no card time.

WHAT PASSES. Every text's cosine to the reference ≥ 0.99 (harrier's bar), on
both sides. Below 0.999 is reported apart: a converter that adds a BOS the
model's tokenizer does not, or pools over the wrong tokens, can stay above
0.99 under mean pooling and still be a different model. Such a text is read
before the rows count.

WHY BOTH SIDES. The query and the document take different prefixes (the
card's "query: " and "passage: " for Nemotron-3-Embed). A prefix put on the
wrong side, or twice, passes every other check and moves one side only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: harrier's conversion bar (gutenberg-encoder-bakeoff.md)
PASS = 0.99
#: below this a text is read before the rows count
READ = 0.999


def pick(out: Path, *, docs: int = 32, queries: int = 16) -> list[dict]:
    """`docs` chunks spread over the sample's lengths (the shortest and the
    longest included) and `queries` questions, the languages alternating as
    the sample has them. Deterministic: the same sample gives the same texts."""
    corpus = [json.loads(line) for line in (out / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
              if line.strip()]
    qs = [json.loads(line) for line in (out / "queries.jsonl").read_text(encoding="utf-8").splitlines()
          if line.strip()]
    chunks = sorted({c for e in corpus for c in e["chunks"]}, key=lambda t: (len(t), t))
    n = min(docs, len(chunks))
    at = sorted({round(i * (len(chunks) - 1) / max(1, n - 1)) for i in range(n)}) if n else []
    picked = [{"side": "document", "text": chunks[i]} for i in at]
    by_lang: dict[str, list[dict]] = {}
    for q in qs:
        by_lang.setdefault(q.get("lang") or "", []).append(q)
    langs = sorted(by_lang)
    k = 0
    while sum(1 for p in picked if p["side"] == "query") < min(queries, len(qs)):
        lang = langs[k % len(langs)]
        if by_lang[lang]:
            picked.append({"side": "query", "text": by_lang[lang].pop(0)["query"], "lang": lang})
        k += 1
    return picked


def read_texts(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def reference(model: str, texts: list[dict], *, query_prompt: str, document_prompt: str,
              max_seq_length: int = 8192):
    """The model as its card runs it, on the CPU in fp32: each text with its
    side's prompt, the pooling and normalisation its saved modules state."""
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer
    torch.manual_seed(0)
    st = SentenceTransformer(model, device="cpu", model_kwargs={"dtype": torch.float32})
    st.max_seq_length = max_seq_length
    prompts = {"query": query_prompt, "document": document_prompt}
    out = []
    for t in texts:
        out.append(st.encode([prompts[t["side"]] + t["text"]], batch_size=1,
                             normalize_embeddings=True, convert_to_numpy=True)[0])
    return np.asarray(out, dtype="float32")


def compare(texts: list[dict], ref, got) -> dict:
    """Each text's cosine to the reference, per side, and the verdict."""
    import numpy as np
    ref = np.asarray(ref, dtype="float64")
    got = np.asarray(got, dtype="float64")
    if ref.shape != got.shape:
        raise SystemExit(f"the reference has shape {ref.shape}, the candidate {got.shape}: "
                         f"another dimension or another text count is another model")
    cos = (ref * got).sum(1) / (np.linalg.norm(ref, axis=1) * np.linalg.norm(got, axis=1))
    res: dict = {"texts": len(texts), "pass_at": PASS, "read_below": READ}
    for side in ("document", "query"):
        idx = [i for i, t in enumerate(texts) if t["side"] == side]
        if idx:
            c = cos[idx]
            res[side] = {"n": len(idx), "worst": round(float(c.min()), 6),
                         "median": round(float(np.median(c)), 6)}
    res["below_pass"] = [i for i, c in enumerate(cos) if c < PASS]
    res["to_read"] = [i for i, c in enumerate(cos) if PASS <= c < READ]
    res["verdict"] = "PASS" if not res["below_pass"] else "FAIL"
    return res


def sent_text(encoder: str, text: str, query: bool) -> str:
    """The exact string the engine receives for `text`, as encoder_fn sends
    it: the recipe's query instruction before a question, the model's
    document prefix before a document."""
    from tools.measure import wikidata_encoder_ab as ab
    if query:
        return (ab._recipe(encoder).query_instruction or "") + text
    if encoder in ("nemotron", "nemotron-m1024"):
        return ab.NEMOTRON_PASSAGE + text
    if encoder == "jina-gguf":
        return ab.JINA_INSTRUCTIONS[False] + text
    return text


def sample_texts(out: Path) -> list[dict]:
    """Every chunk and every question of a wikidata_encoder_ab sample."""
    texts = [{"side": "document", "text": c}
             for line in (out / "corpus.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()
             for c in json.loads(line)["chunks"]]
    texts += [{"side": "query", "text": json.loads(line)["query"]}
              for line in (out / "queries.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return texts


def differ(mine: list[int], theirs: list[int]) -> int:
    """How many tokens of the longer sequence are not in the matched blocks."""
    import difflib
    same = sum(b.size for b in difflib.SequenceMatcher(None, mine, theirs, autojunk=False)
               .get_matching_blocks())
    return max(len(mine), len(theirs)) - same


def tokens(texts: list[dict], *, engine, reference, sent) -> dict:
    """Each text's token ids from `engine(string)` against `reference(string)`
    (the model's tokenizer.json, no specials, as the GGUF adds none), on the
    exact string `sent(text, query)`. Returns the counts and the texts that
    differ, in `pick`'s format, ready for `reference` and `compare`."""
    out: dict = {"texts": len(texts), "tokens": 0, "differ_texts": 0, "differ_tokens": 0,
                 "examples": [], "differing": []}
    for t in texts:
        s = sent(t["text"], t["side"] == "query")
        mine, theirs = list(engine(s)), list(reference(s))
        out["tokens"] += len(theirs)
        if mine != theirs:
            out["differ_texts"] += 1
            out["differ_tokens"] += differ(mine, theirs)
            out["differing"].append(t)
            if len(out["examples"]) < 5:
                out["examples"].append({"text": s[:120], "engine": mine[:40], "model": theirs[:40]})
    return out


def candidate(texts: list[dict], encoder: str):
    """The texts as wikidata_encoder_ab embeds them for `encoder`."""
    from tools.measure import wikidata_encoder_ab as ab
    encode, lease = ab.encoder_fn(encoder)
    out: list = [None] * len(texts)
    with lease:
        for side in ("document", "query"):
            idx = [i for i, t in enumerate(texts) if t["side"] == side]
            if idx:
                for i, v in zip(idx, encode([texts[i]["text"] for i in idx], side == "query")):
                    out[i] = v
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pick")
    p.add_argument("out", type=Path)
    p.add_argument("--docs", type=int, default=32)
    p.add_argument("--queries", type=int, default=16)
    r = sub.add_parser("reference")
    r.add_argument("--model", required=True, help="the HF snapshot directory (a pinned revision)")
    r.add_argument("--texts", type=Path, required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--query-prompt", default="")
    r.add_argument("--document-prompt", default="")
    r.add_argument("--max-seq-length", type=int, default=8192)
    k = sub.add_parser("tokens")
    k.add_argument("--sample", type=Path, action="append", default=[],
                   help="a wikidata_encoder_ab sample directory (repeatable): all its texts")
    k.add_argument("--texts", type=Path, default=None)
    k.add_argument("--tokenizer", type=Path, required=True, help="the snapshot's tokenizer.json")
    k.add_argument("--encoder", required=True)
    k.add_argument("--out", type=Path, required=True, help="the texts that differ (JSON lines)")
    c = sub.add_parser("compare")
    c.add_argument("--texts", type=Path, required=True)
    c.add_argument("--ref", type=Path, required=True)
    c.add_argument("--encoder", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "pick":
        for t in pick(a.out, docs=a.docs, queries=a.queries):
            print(json.dumps(t, ensure_ascii=False))
        return 0
    if a.cmd == "reference":
        import numpy as np
        np.save(a.out, reference(a.model, read_texts(a.texts), query_prompt=a.query_prompt,
                                 document_prompt=a.document_prompt,
                                 max_seq_length=a.max_seq_length))
        print(f"{a.out}: reference written", file=sys.stderr)
        return 0
    if a.cmd == "tokens":
        import httpx
        from tokenizers import Tokenizer

        from tools.measure import wikidata_encoder_ab as ab
        texts = [t for d in a.sample for t in sample_texts(d)] + (read_texts(a.texts) if a.texts else [])
        recipe = ab._recipe(a.encoder)
        tok = Tokenizer.from_file(str(a.tokenizer))
        with httpx.Client(timeout=60.0) as http:
            def engine(s: str) -> list[int]:
                r = http.post(f"{recipe.url}/v1/tokenize", json={"model": recipe.model, "content": s})
                r.raise_for_status()
                return r.json()["tokens"]
            res = tokens(texts, engine=engine,
                         reference=lambda s: tok.encode(s, add_special_tokens=False).ids,
                         sent=lambda text, query: sent_text(a.encoder, text, query))
        a.out.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in res.pop("differing")),
                         encoding="utf-8")
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if not res["differ_texts"] else 1
    import numpy as np
    texts = read_texts(a.texts)
    res = compare(texts, np.load(a.ref), candidate(texts, a.encoder))
    print(json.dumps(res, indent=2))
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
