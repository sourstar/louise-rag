"""MADLAD on our llama.cpp backend with waves (P3 + P4), measured on the node
against the records München kept for P3 and torch (docs/notes/translator-choice.md,
"Waves"; docs/notes/llama-cpp-encoder-decoder.md §6 T3/T4).

    python t5waves.py URL#MODEL QUERIES.jsonl REFERENCE.jsonl OUT_DIR [--n N] [--t4 CMP.jsonl]

QUERIES: {"qid","lang","query"} per line. REFERENCE: torch's translations,
{"qid","lang","model","translations":{code: text|null}} per line (q_madlad.jsonl).
CMP (optional): P3's one-slot answers, {"qid","to","same","torch","llama"} per
line (cmp-card-f16.jsonl). Every query's languages are the reference's.

Writes to OUT_DIR:
  t4.jsonl     one request at a time (a wave of one): llama vs P3's llama (CMP) —
               identical expected — and vs torch;
  t3.jsonl     each query's languages sent at once, TWICE: wave A == wave B
               (the gate) and in-wave vs alone (informational);
  q_waves.jsonl the wave translations in REFERENCE's shape, for
               tools/measure/translator_choice_search.py (the retrieval gate);
  summary.txt  counts and seconds.
Greedy, max_tokens = min(512, 2 * tokens + 16), transformers' clean_up applied.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import time
import urllib.request
from pathlib import Path

CLEAN = ((" .", "."), (" ?", "?"), (" !", "!"), (" ,", ","), (" ' ", "'"), (" n't", "n't"),
         (" 'm", "'m"), (" 's", "'s"), (" 've", "'ve"), (" 're", "'re"))


def clean(text: str) -> str:
    for a, b in CLEAN:
        text = text.replace(a, b)
    return text.strip()


class Server:
    def __init__(self, spec: str) -> None:
        self.url, _, self.model = spec.partition("#")

    def post(self, path: str, body: dict, timeout: float = 600) -> dict:
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def n_tokens(self, prompt: str) -> int:
        return len(self.post("/v1/tokenize", {"model": self.model, "content": prompt})["tokens"])

    def translate(self, code: str, text: str) -> tuple[str, float]:
        prompt = f"<2{code}> {text}"
        n = self.n_tokens(prompt)
        t0 = time.perf_counter()
        got = self.post("/v1/completions", {"model": self.model, "prompt": prompt,
                                            "max_tokens": min(512, 2 * n + 16),
                                            "temperature": 0, "top_k": 1, "seed": 0})
        return clean(got["choices"][0]["text"]), time.perf_counter() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("server")
    ap.add_argument("queries")
    ap.add_argument("reference")
    ap.add_argument("out")
    ap.add_argument("--n", type=int, default=490, help="queries to run (evenly spaced)")
    ap.add_argument("--t4", default=None, help="P3's cmp-card-f16.jsonl: the one-slot bytes to match")
    ap.add_argument("--t4-n", type=int, default=60, help="queries for T4 (one at a time)")
    a = ap.parse_args()
    srv = Server(a.server)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    queries = {q["qid"]: q for q in map(json.loads, open(a.queries, encoding="utf-8"))}
    refs = [json.loads(line) for line in open(a.reference, encoding="utf-8")]
    step = max(1, len(refs) // a.n)
    refs = refs[::step][:a.n]
    summary = []

    # ---- T4: one request at a time, against P3's recorded one-slot answers
    if a.t4:
        p3 = {(r["qid"], r["to"]): r["llama"] for r in map(json.loads, open(a.t4, encoding="utf-8"))}
        same_p3 = same_torch = total = 0
        secs = []
        with open(out / "t4.jsonl", "w", encoding="utf-8") as f:
            for ref in refs[:: max(1, len(refs) // a.t4_n)][:a.t4_n]:
                text = queries[ref["qid"]]["query"]
                for code, want in ref["translations"].items():
                    if want is None or (ref["qid"], code) not in p3:
                        continue
                    got, s = srv.translate(code, text)
                    secs.append(s)
                    total += 1
                    same_p3 += got == clean(p3[(ref["qid"], code)])
                    same_torch += got == want.strip()
                    f.write(json.dumps({"qid": ref["qid"], "to": code, "llama": got,
                                        "p3": p3[(ref["qid"], code)], "torch": want,
                                        "same_p3": got == clean(p3[(ref["qid"], code)])}, ensure_ascii=False) + "\n")
        secs.sort()
        line = (f"T4 one at a time: {same_p3}/{total} identical to P3's one slot "
                f"({'PASS' if same_p3 == total else 'FAIL'}); {same_torch}/{total} identical to torch; "
                f"median {secs[len(secs) // 2]:.2f} s per translation")
        print(line, flush=True)
        summary.append(line)

    # ---- T3: every language of a query at once, twice; and alone
    same_ab = same_alone = total = 0
    q_secs = []
    with open(out / "t3.jsonl", "w", encoding="utf-8") as f3, \
            open(out / "q_waves.jsonl", "w", encoding="utf-8") as fq:
        for ref in refs:
            q = queries[ref["qid"]]
            codes = [c for c, w in ref["translations"].items() if w is not None]
            if not codes:
                continue

            def wave() -> tuple[dict[str, str], float]:
                t0 = time.perf_counter()
                with cf.ThreadPoolExecutor(max_workers=len(codes)) as ex:
                    got = dict(zip(codes, ex.map(lambda c: srv.translate(c, q["query"])[0], codes)))
                return got, time.perf_counter() - t0

            wave_a, sa = wave()
            wave_b, sb = wave()
            q_secs.append(sa)
            alone = {c: srv.translate(c, q["query"])[0] for c in codes}
            for c in codes:
                total += 1
                same_ab += wave_a[c] == wave_b[c]
                same_alone += wave_a[c] == alone[c]
                f3.write(json.dumps({"qid": ref["qid"], "to": c, "wave_a": wave_a[c], "wave_b": wave_b[c],
                                     "alone": alone[c], "torch": ref["translations"][c],
                                     "same_ab": wave_a[c] == wave_b[c], "same_alone": wave_a[c] == alone[c]},
                                    ensure_ascii=False) + "\n")
            fq.write(json.dumps({"qid": ref["qid"], "lang": ref["lang"], "model": "madlad-waves",
                                 "translations": {c: wave_a.get(c) for c in ref["translations"]}},
                                ensure_ascii=False) + "\n")
            f3.flush()
            fq.flush()
            print(f"{ref['qid']}: {len(codes)} languages in {sa:.2f} s (again {sb:.2f} s)", flush=True)
    q_secs.sort()
    line = (f"T3 same group twice: {same_ab}/{total} identical ({'PASS' if same_ab == total else 'FAIL'}); "
            f"in-wave == alone for {same_alone}/{total} (informational); "
            f"median {q_secs[len(q_secs) // 2]:.2f} s per question, p90 {q_secs[int(len(q_secs) * 0.9)]:.2f} s")
    print(line)
    summary.append(line)
    (out / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
