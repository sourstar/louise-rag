"""GutenbergSource — CorpusSource adapter around pg_text/rdf.

discover()/fetch()/parse() satisfy the CorpusSource protocol; read_metadata()/
read_chapters() are extra, non-protocol helpers that worker.py's per-stage
functions call directly so the `rdf` stage never pays for a full-text read and
the `fts`/`embed` stages never re-parse metadata — matching today's exact lazy
I/O cost per stage, which parse() alone (always returning the full
CanonicalDoc) can't express.
"""
import hashlib
import re
import zipfile
import zlib
from pathlib import Path

from .. import cursor
from ..base import (CanonicalDoc, CorpusSource, DiscoverState, DocRef, Erasure,
                    Figure,
                    LicenseClass, Modality, Mutability, PROTOCOL_VERSION,
                    PrivacyClass, RawDoc, UnindexableDocument)
from . import pg_text, rdf


#: The reading rule, in order (operator, 2026-09-11). ONE parser with a
#: declared branch, not two parsers: a verifier applying this rule to the
#: same book directory reaches the same form, so `records-vs-origin` can
#: re-fetch and reproduce. That is what makes a mixed-form corpus legal
#: under invariant 11 — the choice is recorded, and `extra.form` names it
#: per record.
FORMS = ("epub", "txt", "rdf")


def pick_form(book_dir: Path, book_id: int) -> tuple[str | None, Path | None, bool]:
    """(form, path, is_stub) — which edition of this book to read.

    EPUB first, because the plates are only there and because it is the
    form the origin keeps most current. TXT next, which is what this
    collection read until now. RDF last, and it carries no body text at
    all: the record is then title, author and subjects with no `sections`,
    which is a catalogue entry rather than a book. That is worth having —
    ~1,199 Gutenberg entries have nothing else, and the alternative is an
    error row that says the book does not exist.

    `is_stub` is an EMPTY directory: rsync creates the tree and then copies
    only what the filter matches, so a title the mirror skipped leaves one
    behind. Minting a document for it produced ~21,000 permanently errored
    rows on the live node.
    """
    from . import pg_epub

    try:
        entries = sorted(book_dir.iterdir())
    except OSError:
        return None, None, False
    if not entries:
        return None, None, True

    # THE FIRST FORM THAT CAN BE READ, never the first file that exists.
    # Project Gutenberg publishes files that are named like epubs and are
    # not: nine books in the live mirror carry a ZERO-BYTE
    # `pg<id>-images.epub` beside a perfectly good plain one, dated 2009,
    # and `pg2215` blew up `fts` with `BadZipFile` the first time the rule
    # reached for one. `pg_epub.readable` is a pure function of the bytes
    # on disk, so a verifier applying this rule to the same directory
    # reaches the same form — which is what invariant 11 needs of it.
    #
    # `find_epub` takes the ILLUSTRATED edition only. A book published
    # without one still reads better as an epub than as a hard-wrapped
    # .txt, so the plain edition is the next candidate — shortest name
    # first, which is stable and does not depend on directory order
    # (invariant 3).
    illustrated = pg_epub.find_epub(book_dir, book_id)
    plain = sorted((p for p in entries
                    if p.suffix.lower() == ".epub" and p.is_file()
                    and p != illustrated),
                   key=lambda p: (len(p.name), p.name))
    for candidate in ([illustrated] if illustrated is not None else []) + plain:
        if pg_epub.readable(candidate):
            return "epub", candidate, False

    txt, _, _ = pg_text.scan_dir(book_dir, book_id)
    if txt is not None and txt.stat().st_size > 0:
        return "txt", txt, False

    # An empty .rdf is no more a catalogue record than an empty file is an
    # epub. Same rule, same reason.
    for meta in ([book_dir / f"pg{book_id}.rdf"]
                 + [p for p in entries if p.suffix.lower() == ".rdf"]):
        if meta.is_file() and meta.stat().st_size > 0:
            return "rdf", meta, False
    return None, None, False


#: a year of print: 1000-2099, not inside a longer number
_YEAR = re.compile(r"(?<!\d)(1\d{3}|20\d{2})(?!\d)")
_MARC_C = re.compile(r"\$c\s*([^$]*)")


def edition_date(marc: dict, text_header: dict) -> tuple[str | None, str | None]:
    """`(year, where PG says it)` of the physical edition PG transcribed —
    the operator's main date (2026-09-14). Three places, one fact: PG's
    catalogue writes the year alone in marc906 (12,633 books of 79,382),
    the imprint in marc260 (`$aLondon :$bGeorge Bell, $c1851.`, 12,994),
    and newer headers compose both into "Original publication: London:
    George Bell, 1851". None of them is the FIRST edition's date, which PG
    does not record; marc250's edition statement ("Second edition", 346
    books) is kept with every other marc field."""
    for v in marc.get("906", []):
        m = _YEAR.search(v)
        if m:
            return m.group(1), "marc906"
    for v in marc.get("260", []):
        c = _MARC_C.search(v)
        m = _YEAR.search(c.group(1)) if c else None
        if m:
            return m.group(1), "marc260"
    years = _YEAR.findall(text_header.get("Original publication", ""))
    if years:
        return years[-1], "text_header"
    return None, None


class GutenbergSource:
    source_id = "gutenberg"
    license_class = LicenseClass.GREEN
    # 06 §6.1's corpus credit. Green, so nothing compels it — and it is here
    # anyway: a provider that asks for nothing is still the provider, and
    # 02 §2.7 puts a corpus entry in every collection's `credits`.
    corpus_provider = "Project Gutenberg"
    corpus_license = "public-domain"
    corpus_attribution = ("Project Gutenberg — public-domain texts from "
                          "www.gutenberg.org")
    privacy_class = PrivacyClass.PUBLIC
    chunking_profile = "book"
    # Books are published finished. The catalogue grows; a text
    # already in it does not get edited.
    mutability = Mutability.IMMUTABLE
    protocol_version = PROTOCOL_VERSION
    #: 4 since 2026-09-11: the reading rule became EPUB, then TXT, then RDF
    #: (metadata only). Every record this parser produces for a book with an
    #: epub differs from what version 3 produced for it — measured, 0 of 60
    #: books agreed on `content_hash` — so the version must move, and a
    #: collection built with 3 is a different collection, not a stale one.
    #:
    #: 5 since 2026-09-12: the rule takes the first form that can be READ,
    #: where 4 took the first that existed. Project Gutenberg publishes
    #: zero-byte `pg<id>-images.epub` files — nine in the live mirror, each
    #: beside a good plain epub — and version 4 chose them, which is a
    #: record parsed from bytes that are not a book. The version moves for
    #: the same reason as last time: for those nine books the two versions
    #: produce different `origin`, different `origin_hash` and different
    #: `content_hash`, so they are different records and a collection built
    #: with 4 is a different collection.
    #: 6 since 2026-09-12: titles lose the MARC subfield delimiters that
    #: leaked out of PG's RDF (5.86% of them carried one), 09 §9.5 having
    #: said "cleaned" all along. Invariant 3 — bumped explicitly. `title`
    #: IS inside `content_hash`, so this moves one, which costs nothing
    #: here: the v1.0 gutenberg root starts from zero vectors either way
    #: (migration-plan.md step 8).
    #: 7 since 2026-09-14, the fidelity audit's text findings (invariant 11):
    #: an epub's sections come from ITS OWN headings, not from the chapter
    #: regex, which read verse and body paragraphs as titles; a `<br>`
    #: survives as a line break (a stanza is no longer indistinguishable
    #: from a line); `<sup>`/`<sub>` keep their value as ^{…}/_{…}; an `<hr>`
    #: leaves the plain edition's scene break; table cells keep a boundary;
    #: a member is decoded in the encoding it declares; the licence markers
    #: can no longer run across paragraphs; an epub-form record carries its
    #: PG header fields and `extra.form`. The epub cleaning moved, so the
    #: collection that adopts this parser is a new one, with cleaning 3.
    #: 8 since 2026-09-14 evening, the operator's two answers: `date` is the
    #: year of the PHYSICAL EDITION PG transcribed (marc906, marc260 $c or
    #: the header's "Original publication"), PG's own release date staying
    #: in `extra.issued`; and ITALICS are kept, `_…_`, in the epub text as
    #: the plain text already had them — which moves the text, so, like 7,
    #: it reaches records through a new collection (cleaning_version 3).
    PARSER_VERSION = 8
    modality = Modality.TEXT
    # PG does withdraw a text on a copyright claim, and the tombstone
    # stays: a removal is never silent (04 §4.4).
    erasure = Erasure.RETRACTABLE
    native_id_order = "numeric"          # PG book numbers, issued ascending
    default_facets = [
        {"field": "lang", "kind": "flat", "separator": None, "authority": None},
        {"field": "subjects", "kind": "flat", "separator": None,
         "authority": {"name": "LCSH"}},
    ]

    def discover(self, state: DiscoverState):
        root = state.corpus_root
        if not root.is_dir():
            return
        # Scan cursor, like every other plugin: a book directory whose
        # (mtime, size) is unchanged is skipped without stat-ing its
        # contents. Gutenberg was the last plugin re-examining its whole
        # corpus on every scan — 79k directories, which cost ~5 minutes on
        # an idle pool and 25+ minutes once wikivec/pubmed/wikipedia were
        # saturating the same spindles (~150 IOPS, measured).
        #
        # A DIRECTORY's mtime is the right signature because every way this
        # platform writes a book lands with a rename into that directory
        # (the rsync transport stages then os.replace()s), and a rename
        # changes it. An external mirror rewriting a file IN PLACE would
        # not be seen — the Library's "rescan" button clears the cursor for
        # exactly that case.
        progress = cursor.Progress(state, self.source_id)
        stubs = 0
        for entry in root.iterdir():
            if not entry.is_dir() or not entry.name.isdigit():
                continue
            if progress.unchanged(entry):
                continue
            book_id = int(entry.name)
            # One pass answers both questions (see pg_text.scan_dir): where
            # the text is, and whether this is an EMPTY directory — a
            # transfer stub, not a book we hold. rsync creates the directory
            # tree and then copies only files matching the spec filter
            # (*.txt*, *.rdf), so a title published solely as epub/mobi/html
            # leaves an empty one behind. Minting a document for it produced
            # ~21,000 permanently errored rows on the live node that "clear
            # errors & retry" could never fix (the next scan re-applied the
            # error) and that held every progress bar short of 100%.
            form, path, is_stub = pick_form(entry, book_id)
            # staged either way — AFTER its ref is yielded, so the cursor
            # can never remember a book whose row was lost with an
            # interrupted scan (cursor.Progress): a stub examined once should
            # not be re-examined until something lands in it. Staging
            # declines when the directory changed during this scan, so a
            # concurrent rsync cannot make us skip it forever.
            if is_stub:
                stubs += 1
                progress.stage(entry)
                continue
            if form is None:
                # Nothing readable at all — not even an .rdf. After the
                # three-way rule this is a genuinely empty entry rather
                # than "we only take .txt", so the message says so.
                yield DocRef(source_id=self.source_id, native_id=str(book_id),
                             locator=str(entry),
                             error="no epub, txt or rdf")
                progress.stage(entry)
                continue
            st = path.stat()
            # THE LANGUAGE, HERE, because `slicing.slice_hint` reads it from
            # the manifest and gutenberg slices by it. Without it the
            # planning pass of `records build` has no hint and parses every
            # book — measured 2026-09-10 at 19.2 documents/s
            # single-threaded over 77,866 books, 68 minutes, before the
            # packing pass parsed them all again.
            #
            # The cost lands in the right place. This reads the RDF, a few
            # kilobytes of XML beside a text file that averages 300 KB, and
            # only for a book whose directory changed — `progress.unchanged`
            # skips the rest. A parse that would otherwise happen once per
            # book in a serial pass happens once per NEW book in the scan.
            yield DocRef(source_id=self.source_id, native_id=str(book_id),
                         locator=str(entry), mtime=int(st.st_mtime), size=st.st_size,
                         extra={"txt_name": path.name, "form": form,
                                "lang": rdf.parse(entry, book_id)["lang"]})
            progress.stage(entry)
        if stubs:
            # one scan report, not 21,000 documents
            yield DocRef(source_id=self.source_id, native_id="(scan)",
                         locator=str(root), scope="container",
                         error=f"skipped {stubs} empty book director"
                               f"{'y' if stubs == 1 else 'ies'} (transfer "
                               f"stubs: the mirror had no .txt/.rdf to copy)")
        progress.flush()

    def fetch(self, ref: DocRef) -> RawDoc:
        # The rule is re-applied rather than trusting `ref.extra`: a ref can
        # outlive the scan that made it (a queued document, a retry after a
        # sync landed the epub), and the form must be whatever the corpus
        # holds NOW — with `origin_hash` over the file actually read, which
        # is invariant 11's half of the bargain.
        form, path, _ = pick_form(Path(ref.locator), int(ref.native_id))
        if form is None:
            raise UnindexableDocument(
                f"no epub, txt or rdf for book {ref.native_id}")
        h = hashlib.blake2b(digest_size=16)
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        return RawDoc(ref=ref, raw_hash=h.hexdigest(), path=path)

    def parse(self, raw: RawDoc) -> CanonicalDoc:
        native_id = int(raw.ref.native_id)
        book_dir = Path(raw.ref.locator)
        from . import pg_epub

        meta = rdf.parse(book_dir, native_id)
        suffix = raw.path.suffix.lower()
        plates: list = []
        text_header: dict[str, str] = {}
        text_toc: list[str] = []
        if suffix == ".epub":
            # `pick_form` proved this file readable, and between then and
            # now a sync can have replaced it. A file that will not open is
            # a CORPUS FACT — retrying reads the same bytes — so it is
            # raised unprefixed rather than escaping as a stage error,
            # which the Library counts as blocked and which burns a retry
            # (tests/test_unindexable_stability.py). A member whose DEFLATE
            # data is damaged raises zlib.error, which escaped as a stage
            # error and burned the retries of a book whose other forms may
            # be perfectly good (audit, 2026-09-14).
            try:
                # the book's OWN headings cut it (parser 7); the heading
                # paths left with no body go to `text_toc`, as the regex
                # path's contents-page lines do
                sections, text_toc = pg_epub.sections(raw.path)
                found = pg_epub.figures(raw.path)
                text_header = pg_epub.header_fields(raw.path)
            except (zipfile.BadZipFile, zlib.error, pg_epub.EpubError, OSError) as e:
                raise UnindexableDocument(
                    f"{raw.path.name} is not a readable epub for book "
                    f"{native_id} ({type(e).__name__})") from e
            body_text = ""
            # The caption is already an `[Illustration: …]` marker inside
            # `sections`, so it is indexed; only the BYTES are a member.
            plates = [Figure(href=f.href, media_type=f.media_type,
                             sha256=f.sha256, bytes=f.bytes, caption=f.caption,
                             seq=f.seq, carried=f.carried)
                      for f in found]
        elif suffix == ".rdf":
            # Metadata only: a catalogue entry, not a book. `sections` is
            # empty and stays empty — inventing a section from the title
            # would put a chunk in the index that no page of the book
            # contains.
            sections = []
            body_text = ""
        else:
            body_text = pg_text.read_clean(raw.path)
            sections = pg_text.split_chapters(body_text)
            # the PG header's `Key: value` fields — Credits above all,
            # which read_clean discards with the boilerplate around it
            text_header = pg_text.header_fields(raw.path)
            # THE BOOK'S OWN CONTENTS PAGE, which `split_chapters` drops:
            # its lines each match the chapter pattern and leave no body
            # behind, so 415 headings over 400 sampled books went nowhere.
            # In `extra` rather than `sections` on purpose — promise 9
            # requires keeping what the origin said, not embedding it, and
            # a contents page cut into chunks competes with the prose it
            # indexes.
            text_toc = pg_text.headings_without_body(body_text) if body_text else []
        # AUTHORS ALONE (operator, 2026-09-12: "authors are the only
        # authors. illustrators and translators must be credited but as
        # illustrators and translators"). Built from the ROLES, never by
        # splitting the display string — that string now carries "Kemble,
        # E. W. (illustrator)", and §9.5's `creators` is the work's
        # authors, not a rendering of a credits line.
        #
        # Nobody is dropped: every agent is in `extra.creators_detail`
        # with their role, and the display string credits them all.
        creators = list(dict.fromkeys(meta["author_names"]))

        # PG PRODUCERS ARE CREDITED AS PG PRODUCERS (operator,
        # 2026-09-12). One credits list, and every entry in it says what
        # its person did: `creator` for the author, `ill`/`trl`/`edt` for
        # the illustrator, translator and editor, `producer` for the
        # volunteers who transcribed and proofread THIS edition. They are
        # the reason the digital text exists, and leaving them as an
        # unparsed sentence in a header blob credited them less precisely
        # than everyone else in the same record.
        #
        # From both places PG writes them: the text file's `Credits:`
        # field and the RDF's marc508.
        produced = []
        for credit in ([text_header.get("Credits", "")]
                       + list(meta["marc"].get("508", []))):
            produced.extend(pg_text.producers(credit))
        named = {a.get("name") for a in meta["creators_detail"]}
        credits_detail = list(meta["creators_detail"]) + [
            {"name": n, "role": "producer"}
            for n in dict.fromkeys(produced) if n not in named]
        subjects = [s.strip() for s in meta["subjects"].split("|") if s.strip()]
        edition_year, edition_source = edition_date(meta["marc"], text_header)
        return CanonicalDoc(
            uid=f"{self.source_id}:{native_id}",
            title=meta["title"],
            creators=creators,
            language=meta["lang"],
            # §9.5's `date` IS THE WORK'S PUBLICATION DATE — the operator's
            # rule of 2026-09-12 — and for a PG book it is the year of the
            # PHYSICAL EDITION PG transcribed (operator, 2026-09-14: "the
            # main date is of course the edition date of the physical
            # book"). `edition_date` reads it where PG writes it. Parser 7
            # said PG had no such field: a twelve-book sample showed none,
            # and the census of all 79,382 RDF files found marc906 and
            # marc260 on about 13,000.
            #
            # Null where PG does not say, never a release date — that would
            # be wrong by 135 years for gutenberg:17832, Verne's "Une ville
            # flottante" of 1871, released by PG in 2006 — and never a year
            # mined from marc520's generated prose, which nobody asserted.
            date=edition_year,
            subjects=subjects,
            keys=[],
            # `issued` moves here because it is still LOAD-BEARING: the
            # snapshot cutoff (`--until`) is denominated in it. It simply
            # is not the work's date, which rdf.py's own comment has said
            # all along ("never the text's own date").
            # PROMISE 9: everything else the RDF says about this book.
            # Empty values are omitted so a sparse record stays small.
            # `creators_detail` is separate from `creators` because §9.5
            # makes the latter a list of STRINGS — an agent's birth and
            # death years, aliases, PG agent id and ROLE need objects,
            # and an illustrator is not an author.
            extra={k: v for k, v in {
                "bookshelves": meta["bookshelves"],
                # THE DATE THE BOOK ENTERED PG, kept and asked for
                # (operator, 2026-09-12). Promise 9(c) lets us drop the
                # date an entity entered OUR corpus; this is the date it
                # entered THEIRS, which is a fact about this edition and
                # the key `pack build --until` cuts on.
                "issued": meta.get("issued"),
                # which field `date` came from: marc906, marc260 or
                # text_header (the raw values are in `marc`/`text_header`)
                "date_source": edition_source,
                "creators_detail": credits_detail,
                "languages": meta["languages"],
                "subjects_by_scheme": meta["subjects_by_scheme"],
                "alt_titles": meta["alt_titles"],
                # the first title in the origin's own form, where `title`'s
                # cleaning changed it (its title / subtitle line break), and
                # every further dcterms:title (parser 7; both were dropped)
                "title_raw": meta["title_raw"],
                "other_titles": meta["other_titles"],
                "description": meta["description"],
                "toc": meta["toc"],
                "dcmi_type": meta["dcmi_type"],
                "rights": meta["rights"],
                "reading_ease": meta["reading_ease"],
                "text_toc": text_toc,
                # every pgterms:marcNNN by number — 508 is the production
                # credit, 520 a PG-generated summary, 901 cover images.
                # Captured by number so a datafield this code has never
                # heard of survives.
                "marc": meta["marc"],
                # the header fields of the edition read, Credits included —
                # the epub's own since parser 7, the .txt's before
                "text_header": text_header,
                # WHICH FORM the reading rule chose for this book. The
                # module docstring promised it per record and no record
                # carried it (audit, 2026-09-14): a verifier needs it to
                # reach for the same bytes (invariant 11).
                "form": {".epub": "epub", ".rdf": "rdf"}.get(suffix, "txt"),
            }.items() if v not in ("", None, [], {})},
            sections=sections,
            figures=plates,
            raw_hash=raw.raw_hash,
        )

    # --- non-protocol helpers used by worker.py/summarize.py for lazy,
    # granular I/O matching pre-refactor per-stage cost exactly ---

    def read_metadata(self, book_dir: Path, native_id: int) -> dict:
        return rdf.parse(book_dir, native_id)

    def read_text(self, book_dir: Path, native_id: int) -> str:
        """THE SAME RULE `parse` uses, and it must stay that way.

        This is the worker's lazy path — `read_chapters` below feeds the
        fts and embed stages — while `parse` builds the record. If the two
        chose different forms, the index and the records would describe
        different text for one document, and every chunk id would name a
        chunk whose content the record does not contain. Nothing downstream
        could detect it: both halves would be internally consistent.
        """
        from . import pg_epub

        form, path, _ = pick_form(book_dir, native_id)
        if form is None:
            # the CORPUS fact, not a stage failure — see UnindexableDocument
            raise UnindexableDocument(
                f"no epub, txt or rdf for book {native_id}")
        if form == "epub":
            # As in `parse`: a file that will not open is a corpus fact,
            # raised unprefixed, never a stage error that burns a retry.
            try:
                return pg_epub.read_clean(path)
            except (zipfile.BadZipFile, zlib.error, pg_epub.EpubError, OSError) as e:
                raise UnindexableDocument(
                    f"{path.name} is not a readable epub for book "
                    f"{native_id} ({type(e).__name__})") from e
        if form == "rdf":
            return ""       # a catalogue entry has no body; `sections` is []
        return pg_text.read_clean(path)

    def split_text(self, text: str) -> list[tuple[str, str]]:
        return pg_text.split_chapters(text)

    def read_chapters(self, book_dir: Path, native_id: int) -> list[tuple[str, str]]:
        """The sections the fts and embed stages cut — THE SAME ones `parse`
        puts in the record. An epub is cut at its own headings since parser
        7, so it cannot go through `split_text(read_text())` any more: that
        would index a regex's sections while the record held the markup's,
        and every chunk id would name a chunk the record does not contain."""
        from . import pg_epub

        form, path, _ = pick_form(book_dir, native_id)
        if form == "epub":
            try:
                return pg_epub.sections(path)[0]
            except (zipfile.BadZipFile, zlib.error, pg_epub.EpubError, OSError) as e:
                raise UnindexableDocument(
                    f"{path.name} is not a readable epub for book "
                    f"{native_id} ({type(e).__name__})") from e
        text = self.read_text(book_dir, native_id)
        # An rdf-only book has no body. `split_chapters` answers empty text
        # with `[("Text", "")]` — one section holding nothing — which would
        # index an empty chunk and put a hit with no content in every
        # result set. `parse` returns `[]` for the same book, and these two
        # must agree.
        return self.split_text(text) if text.strip() else []

    def read_figures(self, book_dir: Path, native_id: int,
                     hrefs: dict[str, str]) -> dict[str, bytes]:
        """`{hash: href}` -> `{hash: bytes}` — the container pass (09 §9.5),
        which re-opens the book rather than carrying 50 GB of image bytes
        back out of the parse pool.

        ONE open and ONE application of the reading rule for all of a
        book's plates: the most illustrated book in this corpus carries
        3,503 images, and doing it per plate would re-open that zip 3,503
        times.

        `pick_form` again, and not the epub directly: only an epub-form
        book has plates at all, so a book whose form has changed since the
        record was computed refuses here instead of silently reading an
        href out of a different edition.
        """
        import zipfile as _zip

        form, path, _ = pick_form(book_dir, native_id)
        if form != "epub" or path is None:
            raise UnindexableDocument(
                f"book {native_id} is read as {form or 'nothing'} and carries "
                f"no plates; {len(hrefs)} named plate(s) cannot be read from it")
        with _zip.ZipFile(path) as z:
            return {h: z.read(href) for h, href in hrefs.items()}
