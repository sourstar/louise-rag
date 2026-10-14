"""A cited ARTICLE, pinned by key (05 §5.2 facts-by-key; invariant 4: a
pinned block, never a ranked hit). Operator, 2026-10-13: "art. 12 della
legge sull'emigrazione testo unico 13 novembre 1919, n. 2205" found the act
and ranked other acts' articles above its own; the article was never read.

For every citation in the query that names an article (`app/refs/normattiva
.citations`), in every held collection that declares validity:
1. the act, by its URN in the collection's own key table (`validity_works`):
   the URN the citation spells, `urn:nir:stato:<form>:<date>;<number>`, a
   primary-key lookup; where the citation's form is not the origin's (a
   "legge" that is a regio decreto-legge, a "testo unico"), the URN the
   reference source resolved it to (the query's pinned `atto` facts);
2. the version: the one in force on the query's date (`as_of`), else the
   one shown (the last text);
3. the article: that version's chunk whose label names it, read from the
   index stores as the reader reads it (app/reader.py);
4. a REPEALED article ("ARTICOLO ABROGATO …"): also the last version in
   which it still had its text, with that version's dates — what the reader
   who cites a repealed article is almost always asking for.

Bounded: MAX_ARTICLES articles a query. A collection that cannot be read is
skipped, never fatal: the search answers without the pin."""
from __future__ import annotations

import re
from typing import Any, Iterable

#: articles pinned per query, whatever the query cites
MAX_ARTICLES = 4
#: characters of one article's text in the answer's context: the chat runs a
#: 16,384-token context and the ranked passages take 24,000 characters
#: (retrieval.ANSWER_SOURCE_CHARS); a code's article runs past 10,000 (p99 of
#: the codes' articles, 2,943 tokens, review §3.5). Cut, and the cut said.
MAX_CHARS = 4_000
#: "art. 12", "articolo 5-bis", "art.2 comma 3" → `12`, `5-bis`, `2`
_TOKEN = re.compile(r"art(?:icolo|\.)?\s*(\d+)\s*[-_ ]?\s*"
                    r"(bis|ter|quater|quinquies|sexies|septies|octies|novies|decies)?\b", re.I)
#: a label's article: "Legge sull'emigrazione-art. 12", "Art. 12.", "Articolo 12"
_LABEL = re.compile(r"(?:^|[\s\-–])art(?:icolo|\.)?\s*(\d+)\s*[-_ ]?\s*"
                    r"(bis|ter|quater|quinquies|sexies|septies|octies|novies|decies)?\b", re.I)
_REPEALED = re.compile(r"\bARTICOLO\s+ABROGATO\b|^\s*\(?\s*abrogato\s*\)?\s*\.?\s*$", re.I | re.M)


def token(text: str | None) -> str | None:
    m = _TOKEN.search(text or "")
    if not m:
        return None
    return f"{int(m.group(1))}-{m.group(2).lower()}" if m.group(2) else str(int(m.group(1)))


def label_token(label: str | None) -> str | None:
    m = _LABEL.search(label or "")
    if not m:
        return None
    return f"{int(m.group(1))}-{m.group(2).lower()}" if m.group(2) else str(int(m.group(1)))


def repealed(text: str) -> bool:
    body = text.split("\n", 1)[1] if "\n" in text else text
    return bool(_REPEALED.search(body[:300]))


#: the URN:NIR forms a state act is filed under (app/refs/normattiva.py's
#: citation forms, and those a citation names only by title): a citation
#: whose form is not the origin's — "legge" for a regio decreto-legge, a
#: "testo unico" — is found by its date and number under each of them, one
#: primary-key lookup each, with no reference source needed (2026-10-14: the
#: node had never synced normattiva-refs, and the operator's query resolved
#: to nothing)
FORMS = ("legge", "legge.costituzionale", "decreto.legislativo", "decreto.legge",
         "decreto.del.presidente.della.repubblica",
         "decreto.del.presidente.del.consiglio.dei.ministri", "regio.decreto.legge",
         "regio.decreto", "regio.decreto.legislativo", "decreto.legislativo.luogotenenziale",
         "decreto.luogotenenziale", "decreto", "decreto.ministeriale")


def _urns(con, c: dict, facts: Iterable) -> list[str]:
    """The act's URN keys this collection holds for one citation."""
    out: list[str] = []
    if c.get("data"):
        forms = [c["tipo"]] if c.get("tipo") not in (None, "*") else []
        forms += [f for f in FORMS if f not in forms]
        for form in forms:
            key = f"urn:nir:stato:{form}:{c['data']};{c['numero']}"
            if con.execute("SELECT 1 FROM validity_works WHERE key=? LIMIT 1", (key,)).fetchone():
                out.append(key)
                break                        # the citation's own form first, then the first that holds it
    if not out:
        suffix = f"— {c['art']}"
        for f in facts:
            eid = getattr(f, "entity_id", None) if not isinstance(f, dict) else f.get("entity_id")
            label = getattr(f, "label", None) if not isinstance(f, dict) else f.get("label")
            kind = getattr(f, "kind", None) if not isinstance(f, dict) else f.get("kind")
            if kind == "atto" and eid and str(label or "").endswith(suffix) and eid not in out:
                if con.execute("SELECT 1 FROM validity_works WHERE key=? LIMIT 1", (eid,)).fetchone():
                    out.append(eid)
    return out


def _versions(con, work: str) -> list[dict]:
    rows = con.execute("SELECT uid, native_id, valid_from, valid_to, has_text, shown FROM validity "
                       "WHERE work=? ORDER BY valid_from", (work,)).fetchall()
    return [dict(r) for r in rows]


def _chosen(versions: list[dict], as_of: int | None) -> dict | None:
    if as_of is None:
        return next((v for v in versions if v["shown"]), versions[-1] if versions else None)
    live = [v for v in versions if v["valid_from"] <= as_of
            and (v["valid_to"] is None or as_of < v["valid_to"])]
    return live[-1] if live else None


def _article(collection, native_id: int, tok: str) -> dict | None:
    from app import reader
    doc = reader.document(collection, int(native_id))
    for ch in (doc or {}).get("chunks") or []:
        if label_token(ch.get("chapter")) == tok:
            return ch
    return None


def _day(n) -> str | None:
    from app import validity
    return validity.day_text(n) if n else None


def _fact(collection, urn: str, act_label: str, tok: str, version: dict, chunk: dict,
          note: str) -> dict:
    return {"entity_id": f"{urn}~art{tok.replace('-', '')}", "kind": "articolo",
            "source_id": collection.name,
            "label": f"{act_label} — art. {tok} ({note})",
            "fields": {"testo": _capped(str(chunk.get("text") or "")),
                       "in vigore dal": _day(version["valid_from"]),
                       "fino al": _day(version["valid_to"]) or "oggi",
                       "versione": version["uid"], "chunk": chunk.get("chunk_id")},
            "via": None}


def _capped(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return (text[:MAX_CHARS].rstrip() + f"\n[… testo troncato a {MAX_CHARS} di {len(text)} "
            f"caratteri: il testo intero è nel documento]")


def pin(query: str, collections: Iterable, facts: Iterable = (), *,
        as_of: int | None = None) -> list[dict]:
    """The cited articles' texts as pinned facts (PinnedFact's fields, as
    dicts: the answer's `facts` list holds them so)."""
    from app import db, validity
    from app.refs import normattiva as nref
    cited = [c for c in nref.citations(query) if c.get("art")]
    if not cited:
        return []
    facts = list(facts)
    out: list[dict] = []
    for collection in collections:
        if validity.declared(collection) is None:
            continue
        try:
            con = db.connect(collection)
        except Exception:                    # noqa: BLE001 — the search answers without it
            continue
        try:
            for c in cited:
                tok = token(c["art"])
                if not tok:
                    continue
                for urn in _urns(con, c, facts):
                    works = [r[0] for r in con.execute(
                        "SELECT work FROM validity_works WHERE key=?", (urn,))]
                    for work in works:
                        versions = _versions(con, work)
                        v = _chosen(versions, as_of)
                        if v is None:
                            continue
                        title = con.execute("SELECT title FROM documents WHERE uid=?",
                                            (v["uid"],)).fetchone()
                        act_label = (title[0] if title and title[0] else urn).split(" — ")[0]
                        ch = _article(collection, v["native_id"], tok)
                        if ch is not None:
                            note = "testo vigente" if as_of is None else f"testo al {_day(as_of)}"
                            out.append(_fact(collection, urn, act_label, tok, v, ch, note))
                        if ch is None or repealed(str(ch.get("text") or "")):
                            for older in reversed([x for x in versions
                                                   if x["valid_from"] < v["valid_from"]]):
                                och = _article(collection, older["native_id"], tok)
                                if och is not None and not repealed(str(och.get("text") or "")):
                                    out.append(_fact(collection, urn, act_label, tok, older, och,
                                                     "ultimo testo in vigore"))
                                    break
                        if len(out) >= MAX_ARTICLES:
                            return out[:MAX_ARTICLES]
        except Exception:                    # noqa: BLE001 — never fatal to the search
            continue
        finally:
            con.close()
    return out[:MAX_ARTICLES]
