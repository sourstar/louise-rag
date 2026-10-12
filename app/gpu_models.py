"""The System page's GPU panel, by model: what each model loaded in LocalAI
holds on the card, and whether Louise or someone else uses it.

LocalAI is the only owner of the card's memory (docs/notes/vega.md), and its
`GET /system` lists every loaded model with its backend, its process and
`size_vram` — read from the driver's per-process counters, the same ones the
probe reads (measured 2026-10-12: equal to the MiB for all eight models, and
their sum 35 MiB under the card's own count). So the panel needs no
privilege to read other processes: it asks each LocalAI Louise points at.

"Louise" is a model a collection file names (`embed_model`, `query_model`,
`rerank_model`) or Louise's chat or translation model; every other model is
the operator's own. What the card counts and no loaded model holds is
"outside LocalAI": the driver's own buffers, or another program.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

#: per LocalAI asked; the panel reloads every 5 s
TIMEOUT_S = 1.5


@dataclass(frozen=True)
class ModelRow:
    model: str
    backend: str
    vram: int                  # bytes on the card, LocalAI's `size_vram`
    uses: tuple[str, ...]      # "gutenberg: embedding", "chat", … — empty: not Louise's

    @property
    def louise(self) -> bool:
        return bool(self.uses)


def louise_uses(collections, *, chat_model: str = "", translate_model: str = "",
                reading: dict[str, str] | None = None) -> dict[str, list[str]]:
    """model name -> what Louise uses it for, from the collection files and
    the chat and translation settings."""
    out: dict[str, list[str]] = {}

    def add(model, what):
        if model:
            out.setdefault(str(model), [])
            if what not in out[str(model)]:
                out[str(model)].append(what)
    for c in collections:
        e = getattr(c, "embed", None)
        if e is not None:
            same = e.query_model in (None, e.model)
            add(e.model, f"{c.name}: embedding" + (" and queries" if same else ""))
            if not same:
                add(e.query_model, f"{c.name}: queries")
        r = getattr(c, "reranker", None)
        if r is not None and getattr(r, "model", None):
            add(r.model, f"{c.name}: reranking")
    add(chat_model, "chat")
    add(translate_model, "translation")
    for src, model in sorted((reading or {}).items()):
        add(model, f"translation (reading {src})")
    return out


def origins(collections, extra_urls=()) -> list[str]:
    """Every distinct scheme://host:port Louise sends model requests to."""
    urls = []
    for c in collections:
        e = getattr(c, "embed", None)
        if e is not None:
            urls += [e.url, e.query_url]
        r = getattr(c, "reranker", None)
        if r is not None:
            urls.append(r.url)
    urls += list(extra_urls)
    seen: list[str] = []
    for u in urls:
        if not u:
            continue
        p = urlsplit(str(u))
        if p.scheme and p.netloc:
            o = f"{p.scheme}://{p.netloc}"
            if o not in seen:
                seen.append(o)
    return seen


def rows_from(system: dict, uses: dict[str, list[str]], seen: set | None = None) -> list[ModelRow]:
    """A LocalAI `/system` answer as rows. `seen` holds (model, pid, start)
    of rows already taken: two addresses that reach ONE LocalAI (a name and
    an IP, a path prefix) answer the same models, counted once."""
    seen = set() if seen is None else seen
    rows = []
    for m in (system.get("loaded_models") or []):
        proc = m.get("process") or {}
        key = (str(m.get("id")), proc.get("pid"), proc.get("started_at"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(ModelRow(model=str(m.get("id") or "?"), backend=str(m.get("backend") or ""),
                             vram=int(m.get("size_vram") or 0),
                             uses=tuple(uses.get(str(m.get("id")), ()))))
    return rows


def _get_json(url: str):
    """Split out so tests stub the network without patching httpx, which
    TestClient itself runs on (as app/gpu.py does)."""
    import httpx
    r = httpx.get(url, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def panel(collections, *, extra_urls=(), chat_model: str = "", translate_model: str = "",
          reading: dict[str, str] | None = None, get_json=None) -> dict:
    """The breakdown: rows from every LocalAI that answers `/system`, the two
    sums, and the origins that did not answer (named, never silent)."""
    get_json = get_json or _get_json
    uses = louise_uses(collections, chat_model=chat_model, translate_model=translate_model,
                       reading=reading)
    rows: list[ModelRow] = []
    seen: set = set()
    asked, failed = [], []
    for o in origins(collections, extra_urls):
        try:
            system = get_json(o + "/system")
        except Exception as e:   # noqa: BLE001 — a panel line, never the page's failure
            failed.append(f"{o}: {type(e).__name__}")
            continue
        if not isinstance(system, dict) or "loaded_models" not in system:
            continue              # not a LocalAI
        asked.append(o)
        rows += rows_from(system, uses, seen)
    rows.sort(key=lambda r: (-r.vram, r.model))
    return {"rows": rows, "asked": asked, "failed": failed,
            "louise": sum(r.vram for r in rows if r.louise),
            "other": sum(r.vram for r in rows if not r.louise)}


def bar(models: dict, total: int | None, used: int | None) -> dict[str, float] | None:
    """Per cent of the card: Louise's models, others' models, held outside
    LocalAI (used minus every model), free."""
    if not total or used is None or not models.get("asked"):
        return None
    louise, other = models["louise"], models["other"]
    outside = max(0, used - louise - other)
    pct = lambda b: 100.0 * b / total      # noqa: E731
    return {"louise": pct(louise), "other": pct(other), "outside": pct(outside),
            "outside_bytes": outside, "free": max(0.0, 100.0 - pct(louise) - pct(other) - pct(outside))}
