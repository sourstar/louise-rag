# Figures in the cleaned corpus — an open question, and it blocks full import

Raised by the operator, 2026-09-08, and deliberately left open: **what
may go into a cleaned corpus besides text?** Some books cannot be
understood without their plates — architecture, interior design,
technology. PubMed carries chemical structures, reaction schemes, gels
and plots. A patents collection, which is on the roadmap, is one where
the drawings are not illustration at all: they are part of the claim,
and a patent without them is not the patent.

This note frames the decision. It does not take it.

## Where the specification stands today

It has no answer, and no partial answer. The word "image" appears
throughout documents 01–09 and every occurrence is a *container* image.
There is no figure, no picture, no diagram, no binary content of any
kind.

Concretely:

- A record's content is `sections: [[heading, text], …]` and nothing
  else (02 §2.3, 09 §9.5).
- `content_hash` is BLAKE2b-128 over `[title, [[heading, text], …]]`,
  so a figure could change and the record would not count as changed.
- A chunk is a run of text; every chunker in the registry (`fixed`,
  `cdc`, `abstract`, `verbatim`) cuts text (06 §6.3).
- A space is a function from *cleaned text* to vectors (02 §2.4).
- 01's first promise is that only parsed text, vectors and derived
  indexes travel — "never origin files, markup or anything executable".
- Gutenberg's cleaner reads the `.txt`, which never had figures in it;
  the plates live in the epub and the HTML, which 06 §6.4 records as
  deliberately left behind.

So today a figure does not merely go unindexed. It does not exist.

## What has to be decided, not one thing but five

**1. Does a figure travel at all?** If it does, promise 1 needs the
same treatment the sealed-origin member just got (01, amended
2026-09-08): a named exception with its limits, rather than a promise
that is quietly false.

**2. Does it enter `content_hash`?** If figures are content, a changed
figure must make a changed record, or the delta model lies about what
changed. If they do not, a figure can be swapped under a record whose
hash is unmoved — which for a patent drawing is a forgery the
platform would not notice.

**3. What is a figure's unit?** The record's? Its own? The corpus
fidelity invariant wants an `origin_hash` per verifiable thing, and the
rule settled on 2026-09-08 is that the unit of verification is the unit
a query returns.

**4. Is it embedded, and by what?** A text space cannot embed a picture.
Either the figure contributes only its caption and surrounding text to
the vector — cheap, works now — or the collection needs a multimodal
space, which reaches `space_id`, the chunker registry, the calibration
set (512 *texts*), and T1's overlap band, which means nothing for
images.

**5. Whose licence?** This is the one most likely to bite. A PubMed
abstract being freely available says nothing about its figures, which
are frequently held by the publisher under separate terms. Gutenberg's
plates are public domain where the book is. Patent drawings are
generally free. A collection that travels text under one class and
figures under another needs either a per-member class or a class per
collection that takes the stricter of the two.

## Four shapes, with what each costs

**(a) Figures as text only.** The caption, and where the origin supplies
it the alt-text or an OCR transcription, go into `sections` as ordinary
text. No binary travels; nothing in the specification changes; a search
can find "the plan of the Villa Rotonda" by its caption. What is lost is
the picture itself — the reader is told a figure exists and must go to
the origin for it. This is what red-class collections already do for
text, so the pattern exists.

**(b) Figures as an optional member.** Records stay text; each figure is
named in the record by hash and carried in a member of its own —
`figures/<slice>/…` — which a peer may fetch or not, exactly like the
text index or the sealed originals. Text search and vectors are
untouched; a reader that has the member shows the picture. This is the
smallest change that actually delivers the figure, and it composes with
(a) rather than replacing it: the caption is still text, still indexed.
Costs: a member kind, a record field, a licence class that can differ
per member, and an answer to question 2.

**(c) A figure is a record.** Each figure becomes its own document —
its own `uid`, its caption as `sections`, the image as the bytes
`origin_hash` covers. It fits the existing model with surprisingly
little violence, gives every figure a verifiable identity, and makes
"find me diagrams of X" a search over a slice. It multiplies the record
count, and it needs a rule for the relation back to the document the
figure came from — which 05 §5.5's relations member already provides.

**(d) A multimodal space.** `modality` beyond `text`, an encoder that
embeds pictures, chunks that may be figures. This is the only shape that
makes a picture *findable by what it looks like*. It is also by far the
largest: it touches `space_id`, the chunker registry, the calibration
set and envelope, T1, T2 and T3, and the assumption throughout 03 that a
verifier can re-embed a chunk from text it holds. `modality` exists in
the Codex already and was put there for exactly this kind of question,
so the door is open — but nothing behind it is built.

## What this blocks, and what it does not

It blocks **full import** of any corpus whose figures matter, because
importing text-only and adding figures later is not a repair: it changes
`content_hash`, and therefore every record, chunk and vector. That is a
corrective sister at best and a new collection at worst — the whole
corpus re-derived and re-embedded.

It does not block the proof-of-concept import, which can run on
Gutenberg text and PubMed abstracts as the specification stands, and
does not block the migration plan or the code that precedes importing.

The order that follows: decide this before any corpus is imported for
keeps, and prefer deciding it before the record format is implemented,
since questions 2 and 3 land in `content_hash` and the record line.

---

## The design of shape (b), worked out 2026-09-08

Read against all nine documents, with each finding adversarially
verified. This does not take the decision; it prices it, so that the
decision can be taken with the bill in view.

### The one constraint that fixes everything else

> **CORRECTION, 2026-09-08.** The argument below was priced against a
> reading of 09 §9.7 that the operator has since overturned, and it was
> wrong by about 2.5 orders of magnitude. §9.7 then said "one mismatch
> refuses the bundle at T0"; 03 §3.5 and 07 §7.3 both said that RECORD'S
> ROWS are refused and the import exits 0, and the two won. So moving a
> record's `content_hash` costs that record's rows, not every bundle
> containing it. §9.7 has been amended.
>
> **The conclusion still stands, on better ground: a figure is a member,
> not text.** It is named in the record by hash and carried in
> `figures/<slice>/`; the caption stays in `sections` and stays indexed.
> That is a design argument and it does not depend on the price. Anyone
> re-opening this must argue the design — the cost argument below is no
> longer available, and the delta-model argument was always the weaker
> half of it.

**A figure must not enter `content_hash`**, and the reason was thought to
be stronger than "the delta model would lie". 09 §9.7 said, before the
2026-09-08 amendment:

> **possession.json.** … mapping each uid in `uids` to the
> `content_hash` of the record whose text the producer embedded. The
> importer compares every entry with its own record; one mismatch
> refuses the bundle at T0 — vectors of another revision never attach to
> this text.

On that reading, moving `content_hash` did not merely re-chunk locally:
it orphaned that record's bundles NETWORK-WIDE, and every peer that had
not already merged them had to re-embed. That is the sentence the
correction above retires. Under the per-record reading, retrofitting
plates onto Gutenberg with figures inside `content_hash` re-embeds the
records that gained a plate — not the corpus.

(The alternative is to amend `possession.json` to bind a hash of the
cleaned text rather than `content_hash`. That is a coherent design and a
much larger change; it is not free either way, and it should be a
deliberate choice rather than a side effect.)

### What catches a swapped plate instead

    signed snapshot → shard root (BitTorrent v2 pieces root, 09 §9.4)
                    → the record line, byte for byte
                    → the figure's hash, in that line
                    → the figure bytes

Change a plate and its hash in the record line changes; the line is in
the shard file; the shard's `root` is in the signed snapshot, which a
certifier signs. The existing `roots` check catches it. Nothing new is
needed for integrity.

Note the two notions of "changed" this creates, each with its own job:
`delta.changed` (by `content_hash`) means RE-EMBED; a changed shard root
means RE-FETCH. A new plate on unchanged text is the second only, and
the documents must say so or an implementer will conflate them.

### The container, with measured numbers

One file per figure is not available: 04 §4.3 and 09 §9.16 — "Every file
of a member is one BitTorrent v2 torrent". `storage-tiering.md` §3
counts this node's Gutenberg mirror: **142,891 `.jpg` + 132,009 `.png`,
about 275,000 image files**, against **1,591 shards for the whole
Gutenberg pack**. One container per record shard —
`figures/<slice>/NNNNN…` — is 1,591 torrents instead of 275,000, and
matches the records member's granularity exactly.

### The bill, in full

| what | cost |
|---|---|
| `louise-codex` → version 2 | 09 §9.18: "A change to any schema here is a new `version`, never a silent extension" |
| `louise-snapshot` → version 2 | the figures roots have to be listed somewhere signed |
| three closed lists open | 02 §2.6's kinds, 09 §9.16's table, 01's vocabulary — a twelfth kind |
| 01 promise 1 | a second named exception, beside the sealed-origin one |
| 02 §2.3's opening sentence | "Records are the parsed text of documents and nothing else — no markup, no origin files, no local extras" becomes false |
| 02 §2.7's two closed lists | `figures` must be placed on one; a sister may not change it |
| the licence change is a DAUGHTER | 02 §2.7 keeps `license` on the "decides a record byte" side, so it is not a sister |
| red-class pointer records | a caption is text: blank `caption` as `title` is blanked, and null the section index, or the member ships red text |
| the pre-ack rule | becomes per-kind: a green-text/yellow-figures collection has no `LICENSE-TERMS` at all, so a flat allow-list is wrong |
| `records-vs-origin` | reproducing a record line now means recomputing every figure hash — re-fetching every image of every sampled shard |
| `origin.unit` | ambiguous when text and figures come from different kinds of unit (Gutenberg: `.txt` vs an HTML package) |
| a plates-only record | zero chunks, so unreachable by either leg of a search (05 §5.3). Matters for patents |

### The expensive part, and the consolation

Gutenberg's plates live in the HTML and epub that 06 §6.4 records as
deliberately left behind. Figures therefore mean a DIFFERENT ORIGIN
FORM: a full re-fetch and re-parse. Whether that is a daughter (new
books get plates, old ones do not) or a new collection (everything
re-derived) is the operator's call. It is not a corrective sister:
dropping useful content is bad collection design, not a defect.

The consolation is real. `space_id` covers `{cleaning_version, encoder,
layers, space}` and NOT the origin or the parser. If re-parsing the HTML
yields byte-identical cleaned text, every `content_hash` is unchanged
and every existing vector stays admissible under 03 §3.5. **Re-fetch and
re-parse, but no re-embed** — provided figures stay out of
`content_hash`, which is the same decision as the first section.

---

## DECIDED 2026-09-08 by the operator: shape (b)

Figures travel as an optional member. Records stay text; a figure is
named in the record by hash and carried in `figures/<slice>/…`, which a
peer may fetch or not. The caption stays in `sections` and stays
indexed, so (b) composes with (a) rather than replacing it.

Figures do NOT enter `content_hash` — see the first section above: the
`possession.json` binding of 09 §9.7 makes that the difference between
a format change and re-embedding the corpus at every peer.

The bill above is the bill. Nothing is implemented yet: the amendment
touches nine documents, two schema versions and three closed lists, and
it lands before step 8 of the migration plan, not before step 2.

---

## DECIDED 2026-09-08: Gutenberg-with-plates is a NEW COLLECTION

Not a daughter. A daughter's inherited shards keep the mother's roots
(02 §2.2), so books ingested before the transition would never have
plates and books after it would — "has figures" would depend on the
ingest date, which is an inconsistency to explain forever.

Not a corrective sister either: taking the plain-text form was a design
choice, and a cleaning that drops useful content is bad collection
design, not a defect (the operator's ruling, 2026-09-07).

### What that costs, revised down

The operator reports that the NAS already holds most of Gutenberg as
**both epub and txt**. So the expensive half — re-fetching 79k books
from the origin — largely disappears; it is a re-PARSE of files already
on disk.

`space_id` covers `{cleaning_version, encoder, layers, space}` and
neither `origin` nor `parser`, so the new collection lands in the SAME
space as the old one. Admission is `space_id` plus per-record
`content_hash` (03 §3.5). Therefore:

**The whole cost turns on one empirical question.** Does parsing the
epub yield byte-identical CLEANED TEXT to parsing the `.txt`?

- **Identical** — every `content_hash` is unchanged, every existing
  vector is admissible to the new collection, and the work is a re-parse
  and a re-shard. No re-embedding at all.
- **Different** — every record is a new `content_hash`, no vector
  carries, and the corpus re-embeds.

This is testable on a few hundred books, locally, with no network. It
should be done BEFORE any of the figures amendment is written, because
it is the number the whole plan hangs on and it is cheap to get.

Note what it is not: it is not a question about whether the epub is a
better source. It is a question about whether the two parsers, after
cleaning, agree — and if they nearly-but-not-quite agree, the near miss
is worth understanding before it is designed around.

---

## What the corpus actually holds — measured 2026-09-09

Run over `<gutenberg-root>`: 79,292 book directories,
44,477 illustrated epubs, 786,699 images. Every number below is counted,
not estimated.

**A COVER IS NOT A PLATE, and the epub says which is which.** 09 §9.5
defines a figure as "a plate, a diagram or a drawing the origin carries
BESIDE THE TEXT". Gutenberg's cover is none of those: it generates one
for its own catalogue, for nearly every book, illustrated or not.

100 % of illustrated epubs DECLARE their cover — 
`properties="cover-image"` (EPUB 3) and `<meta name="cover">` (EPUB 2) in
almost equal numbers. So it is identified exactly, with no filename
heuristic and no threshold on the image count. `pg_epub.figures()` reads
both and skips it.

That single exclusion is the difference between a plates collection of
44,486 books and one of the books that have plates:

| | books | plates | member | GPU-days |
|---|---|---|---|---|
| any images epub | 44,486 | 786,699 | 51.0 GB | 14.59 |
| **≥ 1 non-cover plate** | **25,488** | **739,771** | **47.8 GB** | **8.11** |
| cover only | 18,988 (42.7 %) | — | — | — |

**NO OTHER NON-CONTENT MARKER EXISTS.** Swept for every semantic tag
EPUB provides. Present: `cover-image`, `cover` (guide), `nav`, `svg`,
`toc`, and one instance each of `text`, `linked_image`, `icon`. There is
no `logo`, `ornament`, `titlepage`, `colophon` or `frontmatter` marker
anywhere — EPUB defines a vocabulary for these and Gutenberg does not use
it. The cover is the only non-content image the format lets us name.

**CAPTIONS DO NOT IDENTIFY ORNAMENTS.** 10.4 % of non-cover images are
under 5 KB, which is ornament-sized, so caption presence was tested as a
filter. It is not one — the small images are captioned MORE often than
the large:

| image size | has `alt` |
|---|---|
| < 5 KB | 58.7 % |
| 5–50 KB | 60.4 % |
| ≥ 50 KB | 43.5 % |

**ABOUT TWO THIRDS OF PLATES CARRY A CAPTION, and it is already indexed.**
53.4 % have an `alt`. Of the rest, 31 % are followed immediately by
caption-shaped text — real captions, "A TABLEAU OF THE DAY.", "THE PET
DOGS OF NEW-YORK PRESENT THEIR COMPLIMENTS…" — which is another ~14 % of
all plates.

The other three fifths of the alt-less ones are NOT captions and a naive
"take the next text" rule would be wrong about them: 55.8 % are followed
by the literal word `back`, a navigation link in Gutenberg's
scanned-page epubs (6,462 occurrences), 9.3 % by body prose, and 3.9 %
by nothing.

**Nothing needs to change for searchability**, and this is the point that
was got wrong first time round: that text is ordinary document text, so
`read_clean` already captures it, it is already in `sections`, already
chunked and already indexed. The reading order also already puts it
where it belongs —

```
[Illustration]

A TABLEAU OF THE DAY.
```

— so a reader, or an LLM given the chunk, sees the caption under its
plate without the parser associating them explicitly.

What an explicit association would buy is precision, not reach, and it
would cost a heuristic: separating a caption from a `back` link needs
length bounds and link-skipping, and **that heuristic would sit inside
`content_hash`** (the marker is in `sections`). It would therefore be
frozen at `create` and unfixable after, and getting it wrong later means
re-embedding. Left alone deliberately.

Capturing Gutenberg's `<p class="caption">` convention was measured
separately and rejected: it rescues 1.1 % of images, because where a
caption paragraph exists the `alt` almost always does too.
(`<figcaption>` appears ZERO times in 4,747 epubs — Gutenberg predates
HTML5.)

**Thumbnail pairs are negligible**: `_sm_`/`_lg_` duplicates of one plate
appear in 2 books out of 5,745. Real, not worth a rule — and note the
hash-dedup in `figures()` does not catch them, since they are genuinely
different bytes.

**The plates are already on disk.** That tree is a full Gutenberg mirror
carrying both `-images.epub` and `-images-3.epub`. The acquisition
filter's `exclude=("*images*",)` applies to the `extras` path and not to
this corpus, so no re-fetch is needed and "nothing is re-downloaded"
needs no exception.
