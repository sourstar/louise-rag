# Does a collection have a *type*? — content classes and the rules they carry

**Status: PROPOSAL for evaluation. Nothing built, nothing agreed.**
Written 2026-08-26, answering the operator's question directly: does a
Louise collection declare a **type** that carries a bundle of common rules —
so that, say, a legal corpus forbids untraced deletion while a corpus of
reviewed science permits retraction, and a corpus of genetic material
rejects text embedding and text reranking outright?

## 1. The short answer: no, and two of your three examples are uncovered

A plugin declares exactly four classifying attributes (`plugins/base.py`,
the `CorpusSource` protocol), and every one is **single-purpose**:

| attribute | values | what it actually gates | enforced |
|---|---|---|---|
| `license_class` | green / yellow / red | P2P sharing, and the acknowledgement gate | boot + test (`licensing.py`) |
| `privacy_class` | public / restricted / never-share | sharing, architecture §9 | **review only** |
| `chunking_profile` | a string | which chunker, hence `space_id` | boot + test |
| `mutability` | immutable / mutable | fixed-width vs content-defined chunking | review |

There is no type, no domain, no content class. And `mutability` is not the
missing one in disguise — its own docstring is emphatic that it is about
**the cost of re-chunking**, not about whether content changes: *"A source
that is mostly-immutable-but-occasionally-revised belongs in IMMUTABLE: this
switch is about the COST of re-chunking, not about the fact of change."*

Measured against the three examples:

- **Legal — no untraced deletion.** Not covered by anything. Nothing
  declares it, nothing checks it, nothing tells a peer.
- **PubMed — retraction is legitimate.** *Implemented, but not declared.*
  `pubmed/source.py` maps `DeleteCitation` onto an errored `DocRef` that
  every `WHERE error IS NULL` gate excludes. That is plugin behaviour, so
  no other stage knows the corpus has retraction semantics, and a peer
  receiving a pack cannot tell either.
- **Genome — text embedding and text reranking are wrong.** Not covered.
  It is handled by a *convention*: `platform.toml`'s
  `[embed_defaults.<plugin>]`, which exists — by its own comment — because
  **two genome collections were created against the node default and pinned
  a text space over DNA before anyone noticed**. The platform hit this exact
  failure and patched it with a better default rather than a check.

So the question identifies two real gaps, one of which has already cost this
project a documented incident.

## 2. But not one type — axes

The instinct is right; the packaging should be resisted. This codebase
already separates `license_class` from `privacy_class` for a reason: a
corpus can be **public but yellow**, or **green but never-share**. Bundling
them into a "type" would have made both unusable.

A "legal type" would accumulate rules that are not co-extensive with legal
material — a corpus of legal *scholarship* is text about law with none of
the authenticity constraints, and a corpus of case law has different
retention rules from statute. The first time someone needs nine tenths of a
type, a type is the wrong shape.

**Proposal: two new orthogonal axes**, each with one job, each enforced
somewhere nameable.

### 2.1 `modality` — what the bytes *are*

    text | dna | (future: image, audio, …)

Not a policy choice: a fact about the data, and the only axis here that is
genuinely type-like. Its job is to make whole model families **wrong**
rather than merely worse:

- an encoder whose training modality differs from the collection's is
  refused, not deprecated — DNA embedded by a language model is not a worse
  vector, it is a meaningless one;
- likewise a reranker: this **absorbs the `rerank_class` proposal** of
  [`reranker-scope.md`](reranker-scope.md) §7, which was the same idea seen
  from one side. One axis, both uses.

**Enforcement: refuse at collection create *and* at boot**, the same
treatment `space_id` gets — which is the strongest fix available for the
incident `platform.toml` records, and strictly better than a default that a
hand-written TOML can silently override.

### 2.2 `erasure` — how content may leave

    append-only | retractable | erasable

- **`append-only`** *(legal)* — nothing leaves without a trace. Amendment
  and repeal are **metadata**, not deletion
  ([`eli-legal-plugins.md`](eli-legal-plugins.md) §5.7: `eli:repeals`
  relates Works, `in_force` is a property of an Expression). A genuine
  removal must be a recorded tombstone carrying a reason, and counted in the
  manifest so its absence is visible.
- **`retractable`** *(PubMed)* — the publisher may assert that a record
  should never have existed. Removal is *correct*, and the existing
  `DeleteCitation` tombstone is exactly this. Note it is still not silent:
  the row survives carrying its marker, which is why a later re-add is
  detected as a change rather than a phantom insert.
- **`erasable`** *(personal data)* — removal may be **mandatory** and must
  leave **no trace**. This is GDPR erasure, and it is a different thing from
  both of the above.

## 3. The finding: two of those values are contradictory, and it matters

`append-only` and `erasable` cannot both hold, and **a real corpus can
demand both**: the Gazzetta Ufficiale's *serie speciale* concorsi and the
*foglio delle inserzioni* are legal publications of record that are also
full of named individuals. An act of publication that must never be
falsifiable, about a person who may demand erasure.

Two consequences worth writing down:

1. **It retroactively justifies the scope decision.** Restricting the
   Gazzetta plugin to Normattiva's normative corpus
   (`gazzetta-plugin.md` §12.5) was taken on privacy and licensing grounds.
   This axis shows the conflict is structural, not merely awkward — the
   excluded material is the material where the two values collide.

2. **Erasure and exchange are incompatible, and that is arithmetic, not
   policy.** In a P2P system you cannot un-publish: once a shard is seeded,
   no manifest edit reaches the peers who hold it. So:

   > **`erasure = erasable` ⇒ `privacy_class = never-share`.**
   > A collection that may have to erase must never be packed, published or
   > seeded.

   Which yields a clean invariant on the pack format: **a pack can only ever
   carry an `append-only` or a `retractable` collection.** That is checkable
   at `pack build` time, and it is the kind of rule that is cheap now and
   unenforceable after the fact.

## 4. Where it is declared, and the tightening ladder

The plugin declares the default, because these are properties of the source.
The collection may **tighten, never loosen** — the same ladder that already
governs trust artefacts (an untrusted signed artefact may tighten a
tolerance, never loosen it) and the licence gate. A hand-populated folder
under the same plugin may contain material the plugin's author never saw.

The **pack manifest carries the resolved values**, so a peer knows the rules
a collection was built under rather than inferring them from its name.

## 5. Enforcement, per the CLAUDE.md convention

Every invariant names how it is held. Proposed:

| rule | how |
|---|---|
| encoder/reranker modality matches the collection's | **boot** + create-time refusal, + test |
| `append-only`: no stage emits a silent removal; a removal is a tombstone with a reason | **test** (a stage that drops a document without a tombstone fails) |
| `erasable` ⇒ `never-share`, and never packable | **boot** + a `pack build` refusal, + test |
| plugin default may be tightened, never loosened, by a collection | **test** |

Notably none of these is review-only, which is the point: `privacy_class` is
review-enforced today and that is exactly why nobody can say with confidence
what it is doing.

## 6. What this deliberately does not propose

- **No free-text `domain` field.** It would become a dumping ground and
  nothing could check it.
- **No model names on the axis.** `modality` names a *class*; which encoder
  and which reranker are chosen, and on what evidence, belong to
  [`embedder-choice-and-supersession.md`](embedder-choice-and-supersession.md)
  and [`reranker-scope.md`](reranker-scope.md).
- **No renaming of `mutability`**, tempting as it is — its docstring is
  clear even though its name is not, and renaming it touches every plugin
  for no behavioural gain.

## 7. Open questions

1. **Is `modality` per collection or per layer?** Amendment 41 allows
   several chunkers in one collection, but there is still **one encoder per
   collection**, so modality is almost certainly per-collection. A corpus
   mixing text and non-text would need two collections — which is probably
   the right answer anyway.
2. **Does `retractable` need to distinguish "retracted but retained" from
   "purged"?** PubMed keeps the tombstone row; a source that truly deletes
   is a third behaviour.
3. **Should `append-only` be checkable by a peer**, not just by the
   producer? The manifest's per-shard merkle roots make *shard* content
   verifiable, but a document that was never included is invisible. A
   declared document count per slice would make silent exclusion detectable
   — which is the same machinery a possession audit would want.
4. **Do these axes belong in `space_id`?** No, on current reasoning: they
   govern policy and model *eligibility*, not the vector-producing function.
   But `modality` is arguably already implied by the encoder identity that
   *is* pinned, so the question is whether the implication should be made
   explicit.
5. **Is `erasable` reachable at all in this architecture**, or should the
   platform simply refuse to host such corpora and say so? §3's arithmetic
   points that way, and an honest "we cannot do this" beats a value that
   cannot be honoured.
