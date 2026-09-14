"""NormattivaSource — CorpusSource over Normattiva's precompiled Akoma Ntoso
collections (dati.normattiva.it), on the generic ELI layer (../eli.py).

THE ORIGIN FORM, and why — invariant 11 wants it recorded beside the
plugin, because a verifier needs it to reproduce our records:

  * AKN, not XML-NIR, JSON, HTML, PDF or the "URI" export. It is the only
    form that carries the structure (partitions, articles, commi, lettere,
    annexes) and the metadata (FRBR identifiers, URN:NIR, ELI, lifecycle,
    modifications, references) together; the portal publishes the profile
    it adopted the standard by.
  * VIGENTE where the origin offers it: what the law says now, one
    Expression per act (gazzetta-plugin.md §6 option (c)). Multivigente
    would put every version of an article into the same ranked list.
    ORIGINALE only for the collections the origin publishes in no other
    form — among them the 124,043 repealed acts, kept because repeal is
    metadata, not deletion (eli-legal-plugins.md §5.7). An act present in
    several collections is held ONCE: vigente before originale, then the
    latest vigenza, then the first path (discovery is deterministic).
  * The precompiled zip, not the per-article API: one GET per collection
    instead of one request per article of 10^5 acts.

THE EXPRESSION is read from the member, never from the request (ELI rule
2): a precompiled vigente zip takes no date, so the point in time is the
one the origin built it at — the member's name says it (`…_VIGENZA_<date>`)
and the act's own ELI alias says which consolidation that is
(`…/CONSOLIDATED/20230701`). Both are in the record.

Normattiva's own notice, surfaced rather than buried: its texts are NOT
OFFICIAL — the printed Gazzetta Ufficiale prevails (TERMS.txt).

Locator: `<zip>::<member>` — one member is one act. `origin` is therefore
`<zip>#<member>` (plugin_io.origin_locator); the member's bytes as stored
are what `origin_hash` covers, base64 included.
"""
import calendar
import hashlib
import re
import threading
import zipfile
from pathlib import Path

from .. import eli
from ..base import (CanonicalDoc, DiscoverState, DocRef, Erasure, LicenseClass,
                    Modality, Mutability, PROTOCOL_VERSION, PrivacyClass, RawDoc,
                    UnindexableDocument)
from . import akn, rendition

_LOCATOR_SEP = "::"

#: `<TIPO>_<YYYYMMDD>_<N>/<dataGU>_<codiceRedazionale>_<VIGENZA_<date>|ORIGINALE>_V<n>.xml`
#: — the (publication date, codice redazionale) pair is the act's unique
#: key (ELI Italy p.9: codice alone repeats across dates, p.8).
_MEMBER = re.compile(
    r"^(?P<dir>[^/]+)/(?P<gu>\d{4}-\d{2}-\d{2})_(?P<codice>[^_/]+)_"
    r"(?P<form>VIGENZA|ORIGINALE)(?:_(?P<vig>\d{4}-\d{2}-\d{2}))?_V(?P<v>\d+)\.xml$")
_FORMS = {"VIGENZA": "vigente", "ORIGINALE": "originale"}
_FORM_RANK = {"VIGENZA": 1, "ORIGINALE": 0}

#: Italy's ELI implementation (ELI_implementation_in_Italy.pdf, 2018): the
#: by-id form is the unique one, the by-type form an alias.
PROFILE = eli.Profile(
    jurisdiction="IT", authority="stato", base="http://www.normattiva.it/",
    uri_template=("http://www.normattiva.it/eli/id/{yyyy}/{mm}/{dd}/"
                  "{codiceRedazionale}/{tipoVersione}"),
    work_key=("date_publication", "id_local"))


def split_locator(locator: str) -> tuple[Path, str]:
    path, _, member = str(locator).rpartition(_LOCATOR_SEP)
    return Path(path), member


# Open zips, by physical identity. A fetch in native-id order (date order)
# walks acts across every collection, so a one-slot cache would reopen a
# 476 MB central directory per document; there are ~24 zips in all.
_zip_lock = threading.Lock()
_zip_cache: dict[tuple, zipfile.ZipFile] = {}


def _read_member(path: Path, member: str) -> bytes:
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _zip_lock:
        zf = _zip_cache.get(key)
        if zf is None:
            for old_key in [k for k in _zip_cache if k[0] == key[0]]:
                _zip_cache.pop(old_key).close()      # the same path, rebuilt
            zf = _zip_cache[key] = zipfile.ZipFile(path)
        return zf.read(member)


def _member_mtime(info: zipfile.ZipInfo) -> int:
    return calendar.timegm(tuple(info.date_time) + (0, 0, 0))


class NormattivaSource:
    source_id = "normattiva"
    LOCATOR_SEP = _LOCATOR_SEP
    ELI_PROFILE = PROFILE
    license_class = LicenseClass.YELLOW    # CC BY 4.0, and an NC window before it (TERMS.txt)
    corpus_provider = ("Normattiva — Presidenza del Consiglio dei Ministri and "
                       "Istituto Poligrafico e Zecca dello Stato")
    corpus_license = "CC-BY-4.0"
    corpus_attribution = ("Normattiva (www.normattiva.it), CC BY 4.0 — testo non "
                          "ufficiale: fa fede la Gazzetta Ufficiale")
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "akn_article"
    # Consolidated text is revised in place with every amendment.
    mutability = Mutability.MUTABLE
    protocol_version = PROTOCOL_VERSION
    #: 1 since 2026-09-14. 2 the same evening: `keys` empty — 1 wrote
    #: unregistered `eli:`/`urn:` ids, which 05 §5.2 forbids. Free: no
    #: collection was ever created from 1.
    PARSER_VERSION = 2
    modality = Modality.TEXT
    erasure = Erasure.RETRACTABLE          # an act withdrawn from the database
    native_id_order = "numeric"            # eli.native_id: publication date first
    default_facets: list = []

    def discover(self, state: DiscoverState):
        """Every zip is enumerated in full each scan — a central directory
        is cheap to read, and the choice between two copies of one act
        needs all of them at once. Refs come out in native-id order."""
        root = state.corpus_root
        if not root.is_dir():
            return
        guard = eli.CollisionGuard()
        best: dict[int, tuple] = {}
        for zpath in sorted(root.rglob("*.zip")):
            try:
                with zipfile.ZipFile(zpath) as zf:
                    infos = zf.infolist()
            except (zipfile.BadZipFile, OSError) as e:
                yield DocRef(source_id=self.source_id, native_id=zpath.name,
                             locator=str(zpath), scope="container",
                             error=f"unreadable zip {zpath.name}: "
                                   f"{type(e).__name__}: {e}"[:200])
                continue
            for info in infos:
                if info.is_dir():
                    continue
                m = _MEMBER.match(info.filename)
                if m is None:
                    if info.filename.lower().endswith(".xml"):
                        yield DocRef(source_id=self.source_id, native_id=info.filename,
                                     locator=f"{zpath}{_LOCATOR_SEP}{info.filename}",
                                     scope="container",
                                     error=(f"{zpath.name}: member name not in the "
                                            f"origin's pattern: {info.filename}")[:200])
                    continue
                nid = eli.native_id(m["gu"], m["codice"])
                other = guard.check(nid, (m["gu"], m["codice"]))
                if other is not None:
                    # eli-legal-plugins.md §5.2: fail loudly, never trust
                    # the arithmetic. The first holder keeps the id.
                    yield DocRef(source_id=self.source_id, native_id=str(nid),
                                 locator=f"{zpath}{_LOCATOR_SEP}{info.filename}",
                                 scope="container",
                                 error=(f"native id {nid} collides: {other[0]} "
                                        f"{other[1]} and {m['gu']} {m['codice']}"))
                    continue
                rank = (_FORM_RANK[m["form"]], m["vig"] or "", int(m["v"]))
                cand = (rank, str(zpath), info.filename, _member_mtime(info), info.file_size)
                held = best.get(nid)
                if held is None or rank > held[0] or (
                        rank == held[0] and (cand[1], cand[2]) < (held[1], held[2])):
                    best[nid] = cand
        for nid in sorted(best):
            _rank, zpath, member, mtime, size = best[nid]
            yield DocRef(source_id=self.source_id, native_id=str(nid),
                         locator=f"{zpath}{_LOCATOR_SEP}{member}",
                         mtime=mtime, size=size)

    def fetch(self, ref: DocRef) -> RawDoc:
        zpath, member = split_locator(ref.locator)
        try:
            data = _read_member(zpath, member)
        except KeyError as e:
            raise UnindexableDocument(f"{member}: not in {zpath.name}") from e
        except (zipfile.BadZipFile, OSError) as e:
            raise UnindexableDocument(f"{zpath.name}: {type(e).__name__}: {e}") from e
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        return RawDoc(ref=ref, raw_hash=digest, data=data)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        zpath, member = split_locator(raw.ref.locator)
        m = _MEMBER.match(member)
        if m is None:
            raise UnindexableDocument(f"member name not in the origin's pattern: {member}")
        try:
            xml, stored = akn.decode_member(raw.data)
            act = akn.parse(xml)
        except akn.AknError as e:
            raise UnindexableDocument(f"{member}: {e}") from e

        gu, codice = m["gu"], m["codice"]
        by_id = f"id/{gu[:4]}/{gu[5:7]}/{gu[8:]}/{codice}"
        aliases = act.frbr.get("work", {}).get("aliases", {})
        returned = aliases.get("eli", "")
        version, version_date = eli.version_of(returned)
        spans = {p: v for p, v in (act.rdfa.get("work", {}).get("spans") or [])}
        type_resource = spans.get("eli:type_document", "")
        urn = aliases.get("urn:nir", "")
        denomination = rendition.denomination(act)

        extra = {
            # ELI rule 5: the fixed keys (eli.EXTRA_KEYS)
            "eli_jurisdiction": PROFILE.jurisdiction,
            "eli_work": PROFILE.absolute("eli/" + by_id),
            "eli_expression": PROFILE.absolute(returned) if returned else None,
            "eli_type_document": type_resource.rsplit("#", 1)[-1] if type_resource else None,
            "eli_number": act.doc_number,
            "eli_date_document": act.doc_date,
            "eli_date_publication": act.publication.get("date") or gu,
            "eli_version": version,
            "eli_version_date": version_date,
            "eli_id_local": spans.get("eli:id_local") or codice,
            "eli_title": (spans.get("eli:title") or "").strip(),
            "eli_uri_schema": spans.get("eli:uri_schema"),
            # the origin's own statement: the collection of repealed acts
            "eli_in_force": False if "abrogati" in zpath.name else None,
            "eli_cites": act.cites,
            # Normattiva's
            "codice_redazionale": codice,
            "urn_nir": urn,
            "denomination": denomination,
            "doc_type": act.doc_type,
            "doc_date_text": act.doc_date_text,
            "doc_title": act.doc_title,
            "publication": act.publication,
            "form": _FORMS[m["form"]],
            "vigenza": m["vig"],
            "member_version": int(m["v"]),
            "collection": zpath.stem,
            "member_encoding": stored if stored != "xml" else None,
            "act_name": act.act_name,
            "frbr": act.frbr,
            "rdfa": act.rdfa,
            "lifecycle": act.lifecycle,
            "workflow": act.workflow,
            "references": act.references,
            "active_modifications": act.active_modifications,
            "passive_modifications": act.passive_modifications,
            "preface_notes": act.preface_notes,
            "preamble": act.preamble,
            "conclusions": act.conclusions,
            "article_notes": [{"article": a.eid or a.num, "notes": a.notes}
                              for a in act.articles if a.notes],
            "repealed_articles": [{"article": a.eid or a.num,
                                   "text": "\n".join(b.text for b in a.blocks if b.text)}
                                  for a in act.articles if rendition.is_repealed(a)],
            "attachments": [{"name": t.name, "frbr_this": t.frbr_this,
                             "has_text": bool(t.blocks),
                             **({"notes": t.notes} if t.notes else {})}
                            for t in act.attachments],
            "meta_other": act.meta_other,
        }
        return CanonicalDoc(
            uid=f"{self.source_id}:{raw.ref.native_id}",
            title=" — ".join(x for x in (denomination, act.doc_title) if x),
            creators=[],
            language="it",
            date=act.doc_date or None,           # the act's own date (date_document)
            subjects=[],
            # NONE YET. 05 §5.2: "a plugin writes only registered ids into a
            # record's `keys`" — a key in any form but the resolver's never
            # matches, silently — and no reference source registers `eli`
            # or `urn`. The identities are all in `extra` (eli_work,
            # eli_expression, urn_nir, codice_redazionale); keys join them
            # when an `eli` reference source exists, as a new collection.
            keys=[],
            extra={k: v for k, v in extra.items() if v not in (None, "", [], {})},
            sections=rendition.sections(act),
            raw_hash=raw.raw_hash,
        )
