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
