"""Switch-over step 3 (docs/notes/vega.md §12): the verification inputs, as the
VeGa node proof's (docs/notes/vega.md §10): docstrings of Python's standard library,
deterministic, nothing from a Louise collection. Writes <out>/texts.json (N
texts) and <out>/pairs.json (N (title, abstract) pairs: a paragraph's first
sentence and the rest of its docstring; the last pair runs past 512 tokens).

    python texts.py <out dir> [N]
"""
import hashlib
import importlib
import inspect
import json
import re
import sys
import warnings

SKIP = re.compile(r"^(test|idlelib|tkinter|turtle|lib2to3|ensurepip|antigravity|this|__)")


def docs() -> list[str]:
    warnings.simplefilter("ignore")
    seen: set[str] = set()
    for name in sorted(sys.stdlib_module_names):
        if SKIP.match(name):
            continue
        try:
            mod = importlib.import_module(name)
        except BaseException:                                  # noqa: BLE001
            continue
        objs = [mod] + [v for _, v in sorted(vars(mod).items(), key=lambda kv: kv[0])
                        if inspect.isclass(v) or inspect.isfunction(v)]
        for o in objs:
            doc = inspect.getdoc(o) or ""
            t = " ".join(doc.split())
            if 80 <= len(t) <= 4000:
                seen.add(t)
    return sorted(seen, key=lambda t: hashlib.sha256(t.encode()).hexdigest())


def main() -> None:
    out, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 64
    all_docs = docs()
    texts = all_docs[:n]
    pairs = []
    for t in all_docs[n:2 * n - 1]:
        head, _, rest = t.partition(". ")
        pairs.append([head + ".", rest])
    pairs.append(["A long one", " ".join(all_docs[2 * n:2 * n + 40])])
    json.dump(texts, open(f"{out}/texts.json", "w"))
    json.dump(pairs, open(f"{out}/pairs.json", "w"))
    print(len(texts), "texts,", len(pairs), "pairs,", sum(map(len, texts)), "characters of text")


if __name__ == "__main__":
    main()
