"""EmbeddedSource — CorpusSource over pre-embedded parquet corpora
(architecture-v2.md §17 amendment 11: format v1).

The corpus IS the published embedding dataset: parquet shards whose rows
carry (id, url, title, text, embedding), with `id = "{article}_{seq}"` and
the rows of one article consecutive. Text travels WITH the vectors, so
possession, FTS and citations work exactly as for any other corpus — and
the co-located `embedding` column is what the genesis "corpus-embedded"
adapter imports, after the same-function proof, instead of re-embedding.

chunking_profile = "verbatim": one parquet row = one section = one chunk.
The producer already chunked; preserving their boundaries 1:1 is what makes
their vectors attach at all.

Layout (the Upstash convention, adopted as format v1's on-disk shape):
    data/{lang}/{NNN}.parquet

Locator (plugin-opaque, survives the manifest): one or more segments
    "<shard path>::<start_row>::<end_row>"     joined by "|"
end_row exclusive; a second segment covers an article whose rows straddle a
shard boundary. Absolute paths, like pubmed's and wikipedia's locators —
manifest rows rebuild refs with nothing but the locator (plugin_io).
"""
import hashlib
import json
import threading
import time
from pathlib import Path

from .. import cursor
from ..base import CanonicalDoc, DiscoverState, DocRef, LicenseClass, PrivacyClass, RawDoc

_SEG_SEP = "|"
_LOCATOR_SEP = "::"

# One-slot shard cache for fetch(): the worker walks documents in native_id
# order, which is shard order, so consecutive fetches hit the same shard —
# without this, EVERY per-article fetch re-parses a ~437 MB parquet file
# (~95k articles per shard would make rdf/fts unusable; pubmed hit the same
# wall and grew the same cache). Only the four text columns are held, never
# `embedding`. Lock because the parallel embed stage fetches from threads.
_shard_cache: dict = {}
_cache_lock = threading.Lock()


def _shard_rows(path: str) -> "object":
    import pyarrow.parquet as pq
    mtime = Path(path).stat().st_mtime_ns
    with _cache_lock:
        hit = _shard_cache.get(path)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    table = pq.read_table(path, columns=["id", "url", "title", "text"])
    with _cache_lock:
        _shard_cache.clear()            # one slot: shards read sequentially
        _shard_cache[path] = (mtime, table)
    return table

# native_id must be int-castable and unique across languages, so it encodes
# (language, article id) as ordinal * 10^10 + article_id. FROZEN: these
# ordinals are part of every uid ever minted; new languages APPEND, existing
# numbers never change or the manifest orphans itself.
LANG_ORDINAL = {
    "en": 0, "de": 1, "fr": 2, "ru": 3, "es": 4, "it": 5,
    "ja": 6, "pt": 7, "zh": 8, "fa": 9, "tr": 10,
}
_ORDINAL_LANG = {v: k for k, v in LANG_ORDINAL.items()}
_LANG_BASE = 10_000_000_000


def encode_native_id(lang: str, article_id: int) -> int:
    return LANG_ORDINAL[lang] * _LANG_BASE + article_id


def decode_native_id(native_id: int) -> tuple[str, int]:
    return _ORDINAL_LANG[native_id // _LANG_BASE], native_id % _LANG_BASE


def split_row_id(row_id: str) -> tuple[str, int]:
    """'12345_3' -> ('12345', 3). Raises on malformed ids — the caller
    decides whether that costs a row or a shard."""
    article, _, seq = row_id.rpartition("_")
    return article, int(seq)


def embed_input(title: str, text: str) -> str:
    """The producer's embedding input, reproduced as OUR chunk text
    (amendment 11): Upstash prefixed each paragraph with its article title.
    The exact separator is confirmed empirically by the genesis sample
    re-encode — a wrong guess here refuses the archive whole and imports
    nothing, so this constant is safe to correct until first import."""
    return f"{title}\n{text}" if title else text


def _lang_of_shard(rel: Path) -> str | None:
    # data/{lang}/NNN.parquet
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "data" and parts[1] in LANG_ORDINAL:
        return parts[1]
    return None


class EmbeddedSource:
    source_id = "embedded"
    # The dataset repo's metadata license does not relicense the CONTENT:
    # Wikipedia text inside stays CC BY-SA (amendment 11).
    license_class = LicenseClass.YELLOW
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "verbatim"

    def discover(self, state: DiscoverState):
        import pyarrow.parquet as pq
        root = state.corpus_root
        if not root.is_dir():
            return
        shards = sorted(p for p in root.rglob("*.parquet") if p.is_file())
        scan = cursor.load(state.state_dir, self.source_id)
        started_ns = time.time_ns()   # see cursor.mark_if_settled
        # carry the last article of a shard into the next shard's scan so a
        # boundary-straddling article becomes ONE ref with two segments
        pending: dict | None = None
        pending_lang: str | None = None
        for shard in shards:
            rel = shard.relative_to(root)
            lang = _lang_of_shard(rel)
            if lang is None:
                yield DocRef(source_id=self.source_id, native_id=shard.name,
                             locator=str(shard), scope="container",
                             error=f"unrecognized shard path (want data/<lang>/*.parquet): {rel}")
                continue
            if cursor.unchanged(scan, shard):
                if pending is not None:
                    yield self._ref(pending, pending_lang)
                    pending, pending_lang = None, None
                continue
            if pending is not None and pending_lang != lang:
                yield self._ref(pending, pending_lang)
                pending, pending_lang = None, None
            st = shard.stat()
            try:
                pf = pq.ParquetFile(shard)
                row = 0
                for batch in pf.iter_batches(columns=["id", "title"], batch_size=4096):
                    ids = batch.column(0).to_pylist()
                    titles = batch.column(1).to_pylist()
                    for rid, title in zip(ids, titles):
                        article, _seq = split_row_id(rid)
                        if pending is not None and pending["article"] == article:
                            seg = pending["segments"][-1]
                            if seg[0] == str(shard) and seg[2] == row:
                                seg[2] = row + 1
                            else:
                                pending["segments"].append([str(shard), row, row + 1])
                            pending["mtime"], pending["size"] = int(st.st_mtime), st.st_size
                        else:
                            if pending is not None:
                                yield self._ref(pending, pending_lang)
                            pending = {"article": article, "title": title or "",
                                       "segments": [[str(shard), row, row + 1]],
                                       "mtime": int(st.st_mtime), "size": st.st_size}
                            pending_lang = lang
                        row += 1
            except Exception as e:  # noqa: BLE001 — a truncated/corrupt shard
                # costs one error ref, never the scan (pubmed/wikipedia rule)
                pending, pending_lang = None, None
                yield DocRef(source_id=self.source_id, native_id=shard.name,
                             locator=str(shard), scope="container",
                             error=f"unreadable shard {rel}: "
                                    f"{type(e).__name__}: {e}"[:200])
                continue
            cursor.mark_if_settled(scan, shard, started_ns)
        if pending is not None:
            yield self._ref(pending, pending_lang)
        cursor.save(state.state_dir, self.source_id, scan)

    def _ref(self, pending: dict, lang: str) -> DocRef:
        try:
            native = encode_native_id(lang, int(pending["article"]))
        except (KeyError, ValueError):
            return DocRef(source_id=self.source_id, native_id=pending["article"],
                          locator=_join_segments(pending["segments"]),
                          error=f"malformed article id {pending['article']!r}")
        return DocRef(
            source_id=self.source_id, native_id=str(native),
            locator=_join_segments(pending["segments"]),
            mtime=pending["mtime"], size=pending["size"],
            extra={"title": pending["title"], "lang": lang,
                   "article": pending["article"]})

    def fetch(self, ref: DocRef) -> RawDoc:
        rows: list[dict] = []
        for path, start, end in split_locator(ref.locator):
            table = _shard_rows(path)
            for i in range(start, min(end, table.num_rows)):
                rows.append({
                    "id": table.column("id")[i].as_py(),
                    "url": table.column("url")[i].as_py(),
                    "title": table.column("title")[i].as_py(),
                    "text": table.column("text")[i].as_py(),
                })
        data = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        return RawDoc(ref=ref, raw_hash=digest, data=data)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        rows = json.loads(raw.data.decode("utf-8"))
        rows.sort(key=lambda r: split_row_id(r["id"])[1])
        title = rows[0]["title"] if rows else ""
        lang = raw.ref.extra.get("lang") or ""
        article = raw.ref.extra.get("article") or ""
        if not lang or not article:
            # ref rebuilt from a manifest row: recover both from native_id
            lang, aid = decode_native_id(int(raw.ref.native_id))
            article = str(aid)
        sections = [("", embed_input(r["title"], r["text"])) for r in rows]
        return CanonicalDoc(
            uid=f"{self.source_id}:{raw.ref.native_id}",
            title=title,
            creators=[],
            language=lang,
            date=None,
            subjects=[],
            keys=[f"wikipedia:{lang}:{article}"],
            extra={"url": rows[0]["url"] if rows else ""},
            sections=sections,
            raw_hash=raw.raw_hash,
        )


def _join_segments(segments: list[list]) -> str:
    return _SEG_SEP.join(
        f"{rel}{_LOCATOR_SEP}{start}{_LOCATOR_SEP}{end}"
        for rel, start, end in segments)


def split_locator(locator: str) -> list[tuple[str, int, int]]:
    out = []
    for seg in locator.split(_SEG_SEP):
        head, _, tail = seg.rpartition(_LOCATOR_SEP)
        path, _, start = head.rpartition(_LOCATOR_SEP)
        out.append((path, int(start), int(tail)))
    return out
