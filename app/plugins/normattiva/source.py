"""NormattivaSource — CorpusSource over Normattiva's precompiled Akoma Ntoso
collections (dati.normattiva.it), on the generic ELI layer (../eli.py).

ONE RECORD PER EXPRESSION (docs/notes/normattiva-multivigenza-review.md §3,
operator's decision 2026-10-12). Every member of the multivigente zips is a
record: the act as originally published (`ORIGINALE`) and each consolidated
text (`VIGENZA_<date>`). A question about the law in force on a date is
answered by validity — `vigenza_from` on the record, its end derived from
the next version's start by the version table the index keeps — never by
similarity between versions (invariant 4) and never by the snapshot chain,
which stays what 02 §2.2 says it is: when we held what the origin had
published.

THE ORIGIN FORM, and why — invariant 11 wants it recorded beside the
plugin, because a verifier needs it to reproduce our records:

  * AKN, not XML-NIR, JSON, HTML, PDF or the "URI" export. It is the only
    form that carries the structure (partitions, articles, commi, lettere,
    annexes) and the metadata (FRBR identifiers, URN:NIR, ELI, lifecycle,
    modifications, references) together. The JSON export was measured
    (review §1): it adds `abrogato: si|no` and nothing on validity ends.
  * MULTIVIGENTE wherever the origin serves it — 22 of its 23 collections,
    7.3 GB, 331,449 members on 2026-10-12 — because it holds every version
    and includes every act's ORIGINALE. ORIGINALE only for the repealed
    acts (`Atti normativi abrogati`), which the origin publishes in no
    other form: a repealed act has no consolidation to produce. Repeal is
    metadata, never deletion (eli-legal-plugins.md §5.7).
  * The precompiled zip, not the per-article API: one GET per collection.

THE VERSION KEY is the origin's own identifier of the Expression, read from
the member name and agreeing with the member's ELI alias (`…/ORIGINAL`,
`…/CONSOLIDATED/20010201`) — NOT its `FRBRExpression` date, which the
review found stale on a real member (`ita@1993-10-10` on the 2001-02-01
version), and never the member's `V<n>`, a per-zip sequence. The id sorts
by the START OF VALIDITY, which is what keeps arrival monotonic (02 §2.2):
a version the origin publishes next month starts next month. An
ORIGINALE's id sorts by its publication date (the member name carries no
other date, and nothing enters into force before it is published, so
arrival stays monotonic); its VALIDITY starts at the Expression date the
member states — `original_start`, the rule review §7 measured on 220
originals of the 22 collections — which the index carries as its own
column beside the id (app/validity.py). The entry into force the origin
states in the preface note is kept beside it as `entry_into_force`; the
Expression date is in `extra.frbr` with the rest of the identification.

A REPEAL STATE is a version like any other: the origin's last word on a
repealed act, every article reading "((PROVVEDIMENTO ABROGATO DAL …))",
dated the day the repeal took effect. It yields no chunk, closes the
version before it, and carries `in_force: false` and the repealing act's
words. 87,798 members of the archive are repeal states (review §1).

THE FLOOR (operator: "from the Constitution onwards"): a version whose
validity ended on or before `VALIDITY_FLOOR` — the next version of the same
act starts on or before it — is not discovered. The 1942 civil code is kept;
a decree repealed in 1935 is dropped; a royal decree repealed by the 2010
taglia-leggi is kept in the text it had until then. Declared pruning
(invariant 11), reproducible from the member list alone, pinned by
PARSER_VERSION; the recipe's `origin.form` names it.

ONE COPY PER EXPRESSION: the repealed acts' originals sit in their own zip
and in `Regi decreti`'s (79,922 acts), and 4,142 acts sit in two live
collections. The copy is chosen deterministically — the M zip before V
before O, then path, then member — and `origin` names the one taken.

Normattiva's own notice, surfaced rather than buried: its texts are NOT
OFFICIAL — the printed Gazzetta Ufficiale prevails (TERMS.txt).

Locator: `<zip>::<member>` — one member is one record. `origin` is therefore
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
from ..base import (CanonicalDoc, DiscoverState, DocRef, Edge, Erasure, LicenseClass,
                    Modality, Mutability, PROTOCOL_VERSION, PrivacyClass, RawDoc,
                    UnindexableDocument)
from . import akn, rendition

_LOCATOR_SEP = "::"

#: `<TIPO>_<YYYYMMDD>_<N>/<dataGU>_<codiceRedazionale>_<VIGENZA_<date>|ORIGINALE>_V<n>.xml`
#: — the (publication date, codice redazionale) pair is the act's unique
#: key (ELI Italy p.9: codice alone repeats across dates, p.8); the form and
#: the date name the Expression; `V<n>` is a per-zip sequence and is not used.
_MEMBER = re.compile(
    r"^(?P<dir>[^/]+)/(?P<gu>\d{4}-\d{2}-\d{2})_(?P<codice>[^_/]+)_"
    r"(?P<form>VIGENZA|ORIGINALE)(?:_(?P<vig>\d{4}-\d{2}-\d{2}))?_V(?P<v>\d+)\.xml$")
_FORMS = {"VIGENZA": "vigente", "ORIGINALE": "originale"}
#: which zip's copy of one Expression is held: the zip's form is the
#: directory the acquisition spec lands it in (`akn/<M|V|O>/…`)
_ZIP_FORM_RANK = {"M": 0, "V": 1, "O": 2}

#: A version whose validity ends on or before this day is not discovered.
VALIDITY_FLOOR = "1948-01-01"

#: "Entrata in vigore del provvedimento: 17/03/1861" — the preface note the
#: origin writes on most acts; kept as `entry_into_force`.
_ENTRY_INTO_FORCE = re.compile(r"Entrata in vigore[^:]*:\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")

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


def version_key(form: str, vig: str | None) -> str:
    """The origin's identifier of the Expression, from the member name: the
    tail of its ELI alias — `ORIGINAL`, or `CONSOLIDATED/<yyyymmdd>`."""
    if form == "ORIGINALE" or not vig:
        return "ORIGINAL"
    return f"CONSOLIDATED/{vig.replace('-', '')}"


def member_dir_type(member_dir: str) -> str:
    """`LEGGE COSTITUZIONALE_19480226_3` → `LEGGE COSTITUZIONALE`: the act
    type the origin files the member under (the slice's type, pack/slicing)."""
    parts = member_dir.split("_")
    return "_".join(parts[:-2]).strip() if len(parts) >= 3 else member_dir.strip()


def original_start(expression_date: str | None, entry_into_force: str | None, gu: str) -> str:
    """An ORIGINALE's start of validity (review §7, measured 2026-10-12 on
    220 originals, ten per collection): the Expression date the member
    states IS its entry into force — equal to the preface's "Entrata in
    vigore" note on 151 of the 154 members carrying one, and on the pattern
    of the vacatio (publication + 15, + 1, + 0) where the note is absent.
    The two disagreements favour the Expression date (an entry into force
    postponed after the note was written; a note dated to the wrong
    century). The one member whose Expression date PRECEDES publication (a
    typo at the origin, 1989-02-16 for 1989-03-16) takes the note, which
    agrees with the first consolidation; without a note, the publication
    date. Nothing enters into force before it is published."""
    if expression_date and expression_date >= gu:
        return expression_date
    if entry_into_force and entry_into_force >= gu:
        return entry_into_force
    return gu


def _entry_into_force(notes: list[str]) -> str | None:
    for note in notes:
        m = _ENTRY_INTO_FORCE.search(note)
        if m:
            d, mo, y = m.groups()
            return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


# Open zips, by physical identity. A fetch in native-id order (date order)
# walks acts across every collection, so a one-slot cache would reopen a
# 1.7 GB central directory per document; there are ~23 zips in all.
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


def _zip_rank(zpath: Path) -> int:
    return _ZIP_FORM_RANK.get(zpath.parent.name, len(_ZIP_FORM_RANK))


class NormattivaSource:
    source_id = "normattiva"
    LOCATOR_SEP = _LOCATOR_SEP
    ELI_PROFILE = PROFILE
    VALIDITY_FLOOR = VALIDITY_FLOOR
    #: 09 §9.3's `profile`, copied into the Codex at `create`: the ELI
    #: implementation (eli-legal-plugins.md §5.3) and the validity axis of
    #: one record per Expression (app/validity.py; review §4.2)
    default_profile = {
        "eli": {"jurisdiction": PROFILE.jurisdiction, "authority": PROFILE.authority,
                "uri_template": PROFILE.uri_template, "work_key": list(PROFILE.work_key)},
        "validity": {"kind": "versions-per-work", "from": "extra.vigenza_from",
                     "work": "extra.eli_work"},
    }
    license_class = LicenseClass.YELLOW    # CC BY 4.0, and an NC window before it (TERMS.txt)
    corpus_provider = ("Normattiva — Presidenza del Consiglio dei Ministri and "
                       "Istituto Poligrafico e Zecca dello Stato")
    corpus_license = "CC-BY-4.0"
    corpus_attribution = ("Normattiva (www.normattiva.it), CC BY 4.0 — testo non "
                          "ufficiale: fa fede la Gazzetta Ufficiale")
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "akn_article"
    # The origin revises a published version in place when it corrects it.
    mutability = Mutability.MUTABLE
    protocol_version = PROTOCOL_VERSION
    #: 1 since 2026-09-14. 2 the same evening: `keys` empty — 1 wrote
    #: unregistered `eli:`/`urn:` ids, which 05 §5.2 forbids. 3 on
    #: 2026-10-12: one record per Expression (the id, `vigenza_from`,
    #: `version_key`, `member_dir`, the repeal state, `entry_into_force`),
    #: and `in_force` where 2 wrote `eli_in_force` (09 §9.5's key). Free: no
    #: collection was ever created from 1 or 2.
    #: 4 the same day: `keys` carry the act's URN:NIR and ELI, registered
    #: by app/refs/normattiva.py (05 §5.2); free for the same reason. 5 the
    #: same evening: an ORIGINALE's `vigenza_from` is its Expression date
    #: (`original_start`, measured); free for the same reason.
    PARSER_VERSION = 5
    modality = Modality.TEXT
    erasure = Erasure.RETRACTABLE          # an act withdrawn from the database
    native_id_order = "numeric"            # eli.expression_native_id: start of validity first
    default_facets: list = []

    def discover(self, state: DiscoverState):
        """Every zip is enumerated in full each scan — a central directory
        is cheap to read, and both the choice between two copies of one
        Expression and the floor need every member of an act at once. Refs
        come out in native-id order, which is validity order."""
        root = state.corpus_root
        if not root.is_dir():
            return
        guard = eli.CollisionGuard()
        held: dict[int, tuple] = {}            # nid -> (rank, zpath, member, mtime, size)
        by_act: dict[tuple[str, str], list[tuple]] = {}   # (gu, codice) -> [(from, form order, nid)]
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
            zrank = _zip_rank(zpath)
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
                gu, codice, form, vig = m["gu"], m["codice"], m["form"], m["vig"]
                valid_from = vig or gu
                expr = version_key(form, vig)
                nid = eli.expression_native_id(valid_from, (gu, codice), expr)
                other = guard.check(nid, (gu, codice, expr))
                if other is not None:
                    # eli-legal-plugins.md §5.2: fail loudly, never trust
                    # the arithmetic. The first holder keeps the id.
                    yield DocRef(source_id=self.source_id, native_id=str(nid),
                                 locator=f"{zpath}{_LOCATOR_SEP}{info.filename}",
                                 scope="container",
                                 error=(f"native id {nid} collides: {other[0]} "
                                        f"{other[1]} {other[2]} and {gu} {codice} {expr}"))
                    continue
                rank = (zrank, str(zpath), info.filename)
                cand = (rank, str(zpath), info.filename, _member_mtime(info), info.file_size)
                prior = held.get(nid)
                if prior is None:
                    by_act.setdefault((gu, codice), []).append(
                        (valid_from, 0 if form == "ORIGINALE" else 1, nid))
                if prior is None or rank < prior[0]:
                    held[nid] = cand
        # the floor: a version whose successor starts on or before it ended
        # before the Republic; the last version of an act is open and stays
        dropped: set[int] = set()
        for versions in by_act.values():
            versions.sort()
            for (_, _, nid), (nxt, _, _) in zip(versions, versions[1:]):
                if nxt <= VALIDITY_FLOOR:
                    dropped.add(nid)
        for nid in sorted(held):
            if nid in dropped:
                continue
            _rank, zpath, member, mtime, size = held[nid]
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

        gu, codice, form, vig = m["gu"], m["codice"], m["form"], m["vig"]
        by_id = f"id/{gu[:4]}/{gu[5:7]}/{gu[8:]}/{codice}"
        aliases = act.frbr.get("work", {}).get("aliases", {})
        returned = aliases.get("eli", "")
        version, version_date = eli.version_of(returned)
        spans = {p: v for p, v in (act.rdfa.get("work", {}).get("spans") or [])}
        type_resource = spans.get("eli:type_document", "")
        urn = aliases.get("urn:nir", "")
        denomination = rendition.denomination(act)

        # the repeal state: every article that has text is a repeal statement,
        # and no annex has text — the origin's last word on a repealed act
        with_text = [a for a in act.articles if any(b.text.strip() for b in a.blocks)]
        repealed_in_full = bool(with_text) and all(rendition.is_repealed(a) for a in with_text) \
            and not any(b.text.strip() for t in act.attachments for b in t.blocks)
        repealed_by = ""
        if repealed_in_full:
            first = next(b.text for b in with_text[0].blocks if b.text.strip())
            repealed_by = rendition.plain(first).strip()
        in_force = False if (repealed_in_full or "abrogati" in zpath.name) else None
        entry_into_force = _entry_into_force(act.preface_notes)
        expression_date = act.frbr.get("expression", {}).get("date") or None

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
            "eli_cites": act.cites,
            # 09 §9.5's force status, on the origin's word only
            eli.IN_FORCE: in_force,
            # the Expression this record is (review §3.1), from the member name
            "version_key": version_key(form, vig),
            "vigenza_from": vig or original_start(expression_date, entry_into_force, gu),
            "form": _FORMS[form],
            "vigenza": vig,
            "member_version": int(m["v"]),
            "member_dir": m["dir"],
            "collection": zpath.stem,
            "repealed_in_full": repealed_in_full,
            "repealed_by_text": repealed_by,
            "entry_into_force": entry_into_force,
            # Normattiva's
            "codice_redazionale": codice,
            "urn_nir": urn,
            "denomination": denomination,
            "doc_type": act.doc_type,
            "doc_date_text": act.doc_date_text,
            "doc_title": act.doc_title,
            "publication": act.publication,
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
            # 05 §5.2: only ids a reference source registers — `urn` and
            # `eli` are, since app/refs/normattiva.py (parser 4): the act's
            # URN:NIR verbatim (its scheme is the namespace) as the resolver
            # emits it, and the unique ELI by-id form (eli rule 1); sorted by
            # bytes (09 §9.5)
            keys=sorted({k for k in (urn, PROFILE.key(by_id)) if k}),
            extra={k: v for k, v in extra.items() if v not in (None, "", [], {})},
            sections=rendition.sections(act),
            raw_hash=raw.raw_hash,
        )

    def relations(self, doc: CanonicalDoc):
        """06 §6.1's typed edges, named after ELI properties
        (eli-legal-plugins.md §5.4, 09 §9.14): what THIS version's own
        metadata states. `eli:amends` — or `eli:repeals` where the statement
        says so — for each act its active modifications touch, by the act's
        URN:NIR; `eli:cites` for each act its references point at, by its
        AKN work path. `to` is a canonical id left unresolved (05 §5.5):
        `urn:nir:…`, `akn:it/act/…`; the article a statement names is in the
        record's `active_modifications` and `eli_cites`, the edge is between
        acts. One edge per (type, act)."""
        x = doc.extra or {}
        seen: set[tuple[str, str]] = set()
        for mod in x.get("active_modifications") or []:
            dest = str(mod.get("destination") or "").split("#", 1)[0].strip()
            if not dest.startswith("urn:"):
                continue
            words = " ".join(str(mod.get(k) or "") for k in ("new", "old", "type")).upper()
            kind = "eli:repeals" if "ABROG" in words or "REPEAL" in words else "eli:amends"
            if (kind, dest) in seen:
                continue
            seen.add((kind, dest))
            yield Edge(to=dest, type=kind)
        for href in x.get("eli_cites") or []:
            path = str(href).split("#", 1)[0].split("/!", 1)[0].strip("/")
            if not path.startswith("akn/"):
                continue
            target = "akn:" + path[len("akn/"):]
            if ("eli:cites", target) in seen:
                continue
            seen.add(("eli:cites", target))
            yield Edge(to=target, type="eli:cites")
