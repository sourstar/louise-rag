# Where does a reranker belong? — pack, plugin, or node

**Status: PROPOSAL for evaluation. Nothing built, nothing agreed.**
Written 2026-08-26, prompted by the question the Gazzetta plugin raised
(`gazzetta-plugin.md` §7.4) and deliberately answered in general terms: if a
plugin has a reranker that suits its corpus, should a **pack** be able to
carry it — tied, or recommended?

Short answer: **recommended, never tied — and "recommended" should mean
signed evidence, not configuration.** The reasoning below matters more than
the verdict, because two of the three arguments generalise to anything else
a pack might one day want to tell a consumer to do.

---

## 1. A reranker is not the same class of thing as anything a pack carries

The manifest (`pack-format.md` §7) carries exactly three kinds of field:

| class | examples | why it must travel with the data |
|---|---|---|
| **identity / correctness pins** | `space_id`, `layers`, `cleaning_version`, `dim`, `dtype` | without them the bytes are **uninterpretable** — vectors cut by an unknown chunker, in an unknown space, are noise |
| **verification contracts** | `calibration` + `tolerance`, `terms_sha256`, merkle roots | they let the receiver **check** something they could not otherwise check |
| **coordination metadata** | `update.cadence`, `coordinator`, `members` | they say how the artefact is kept alive and distributed |

A reranker is in none of these classes.

- It is not a correctness pin: the pack's vectors mean exactly the same
  thing whether the consumer reranks with model X, model Y, or not at all.
  Contrast the encoder, which the pack *must* declare — that is what
  `space_id` is for, and getting it wrong silently corrupts retrieval.
- It verifies nothing.
- It coordinates nothing.

**A pack describes data. A reranker is a property of the query path.** That
is the prima facie architectural objection, and it holds regardless of how
good the recommendation is.

## 2. But the producer does hold a signal the consumer cannot cheaply re-derive

The counter-argument is real and should not be waved away. Whoever built a
pack knows things the consumer does not: the corpus's chunk shape, its
query mix, and — if they were rigorous — a *measured* retrieval evaluation
on it. "We measured nDCG@10 at 0.41 fused, 0.52 with reranker X over 40
candidates" is genuinely useful, genuinely expensive to reproduce, and
genuinely the producer's to state.

That is **evidence**, though, not configuration. The distinction is the
whole answer:

> A pack may carry what its publisher **measured**.
> A pack may not carry what the consumer's node **does**.

And the codebase already has this exact shape, in `platform.toml`:
`[trust] space_authors` names keys whose signed space artefacts apply *as
measured*; everyone else's load as **advisory** — able to tighten a
tolerance, never to loosen one. A reranker hint is the same ladder with the
teeth removed entirely, because nothing about a reranker can ever be
binding.

## 3. Three reasons "tied" is wrong, in decreasing order of severity

### 3.1 Security: a pack must never name an endpoint the node calls

This one is decisive on its own. A pack is an artefact from a **peer**,
verified only in the sense that its signature matches a key — which says
who wrote it, not that they are honest. `_rerank` (`app/retrieval.py:77`)
POSTs `{"query", "texts"}` to `RERANK_URL`. So a manifest field that could
set that URL would mean: **a remote publisher chooses a host to which this
node sends the user's query together with the passages it retrieved.**

That is a query-exfiltration channel wearing a configuration field's
clothes, and it would be the first place in the system where received data
selects an outbound destination. The rule to write down, wider than
rerankers:

> **A pack may carry a model *identity* — a name and a digest. It may never
> carry a URL, host, or path the node will contact.**

Same instinct as invariant 8 (secrets by path, never by value) and as the
advisory-only trust ladder: an untrusted artefact may inform a decision, it
may never *be* the decision.

### 3.2 Federation: per-pack rerankers produce scores that do not compare

`_rerank` runs **once**, after `_merge_rrf_federated`, over the fused list
from every queried collection (`retrieval.py:333`). That is not an
implementation accident — it is what makes one ordering across collections
meaningful.

If each pack brought its own reranker, a federated query has N scorers and
two bad options:

1. **Rerank per collection, then fuse the reranked lists.** This discards
   precisely the cross-collection calibration the reranker existed to
   provide, and re-introduces the problem RRF was chosen to solve.
2. **Pick one of the N.** At which point the other N−1 declarations were
   decoration — and worse, *silent* decoration: a field that is present,
   plausible, and inert is the most expensive kind of configuration,
   because someone will set it and believe it took effect.

This is invariant 4's warning one level down. That invariant says two
ranked lists answering different questions must not be fused. Here it is
sharper still: scores from two different scorers are not on a common scale
at all, so they cannot be fused even in principle.

### 3.3 Config tiers: received data is not desired state

Invariant 10 fixes three tiers — env is identity, TOML files are desired
state, DB settings are runtime overrides that do not survive restart — and
forbids writing desired state into the DB. A pack is a **fourth** thing the
tier model does not currently name: data received from a producer.

Nothing in the model lets received data become desired state, and it should
stay that way. A pack's recommendation must land somewhere an operator
**explicitly accepts** it — a TOML edit they make — not somewhere it takes
effect at import. Anything else means a peer's artefact silently changed
how this node answers questions.

## 4. The prior question: today this is not expressible at all

Worth stating plainly before choosing a design, because it changes what is
being asked. **`RERANK_URL` is Tier-1 env and node-global** (`config.py:32`).
It is not in `EmbedRecipe`, not on `Collection`, not in `platform.toml`.
Unlike the encoder — which `collections.recipe_for()` resolves per
collection through a three-step fallback — the reranker has no per-anything
scope whatsoever.

So "a reranker tied to a pack" is not merely inadvisable; it is currently
inexpressible, and the real proposal on the table is *how much scope to give
the reranker at all*. Three options:

**Option A — keep it node-global; packs carry evidence only. (Recommended.)**
The manifest gains an optional advisory hint (§5). The Library card shows it.
An operator reads it and decides for the node. **Zero change to the query
path**, zero new failure modes, and the producer's measurement is still
delivered — which was the entire legitimate motivation.

**Option B — per-collection reranker, node-global in federation.**
More expressive, and it buys: two code paths; the incomparable-scores
problem of §3.2 the moment a second collection is queried; and a
per-collection field that is *silently ignored* in the federated case. I
would not build this without a measured case that a single node reranker
demonstrably fails on some collection.

**Option C — per-plugin default, mirroring `[embed_defaults.<plugin>]`.**
Tempting because that pattern exists and works. But note the disanalogy that
justified it: a genome collection embedded with a text encoder is not
*worse*, it is **wrong** — DNA is not a language and the spaces are not
comparable, which is why that default exists at all (`platform.toml.example`
records the two collections that pinned a text space over DNA before anyone
noticed). For text rerankers there is no wrongness, only better and worse.
The pattern does not transfer on its own merits.

## 5. What a hint would look like, if Option A is accepted

Optional, advisory, additive to the manifest:

```json
"rerank_hint": {
  "model": "jinaai/jina-reranker-v3",
  "digest": "sha256:…",
  "shape": "listwise",
  "eval": {"set": "JuriFindIT-expert", "metric": "ndcg@10",
           "baseline_fused": 0.41, "with_hint": 0.52,
           "candidates": 40, "truncation_chars": null},
  "measured_by": "<publisher pubkey>"
}
```

Properties that make it safe:

- **No URL.** Model identity and digest only (§3.1).
- **Signed for free.** `manifest.canonical_bytes` signs every field but
  `signature`, so an added field is covered automatically, and a reader
  that ignores it still verifies the signature correctly. Backward
  compatible in both directions.
- **Inert on import.** Displayed, never applied. Accepting it is an operator
  action, and the thing they edit is env or TOML — not the pack.
- **Trust-laddered.** From a key in `[trust] space_authors`, show it as
  measured. From anyone else, show it as *claimed, unverified* — the same
  distinction that already governs space artefacts.
- **Carries its conditions.** `candidates` and `truncation_chars` are part
  of the claim, because a reranker's gain is not a property of the model
  alone (§6).

Adding a field to a signed wire format is an "ask first" change under
CLAUDE.md, and whether it needs `VERSION: 2` is an operator call — the
signature argument above suggests it does not.

## 6. Why the consumer must measure anyway, and the hint stays a hint

A reranker's measured gain depends on at least three things the producer
does not control:

1. **The candidate budget** — `max(k * 4, 40)` (`retrieval.py:234`, `:333`).
   Reranking 40 candidates and reranking 200 are different experiments.
2. **The truncation** — `h["text"][:2000]` (`retrieval.py:83`), a hard
   constant, roughly 400 tokens at this repo's measured chars/token. A
   model with 32k of context judging a chunk cut at 2000 characters is not
   the model the producer benchmarked.
3. **The query mix.** The producer's eval set is not this node's users.

So even a perfectly honest, signed, reproduced hint is a *prior*, not a
result. Which is another way of saying Option A is not a compromise — it is
the accurate model of what the producer actually knows.

## 7. When a plugin-level declaration *would* earn its place

There is one case where this stops being about quality, and the test is
worth writing down:

> Declare a reranker at the plugin level only when the general-purpose
> model would be **wrong**, not merely worse.

`genome` is the clean example: a text cross-encoder scoring DNA sequences is
not a weaker reranker, it is a category error — the same argument that
gave genome its own encoder default. A legal or biomedical corpus is *not*
such a case: a general multilingual reranker is worse there, not wrong.

If that case is ever real, the right shape is a **class**, not a model: the
plugin declares `rerank_class = "text" | "dna" | null`, and the node warns
when its configured reranker is of the wrong class. That is a wrongness
check, cheap and static — not a quality knob, and not a per-collection
endpoint.

## 8. What this proposal deliberately does not propose

- No `rerank_url` anywhere in a pack, ever (§3.1).
- No automatic application of a hint on import (§3.3).
- No per-collection reranker until a measured case demands it (§4, Option B).
- No change to the invariant that **pinned facts bypass RRF and rerank
  both** (`retrieval.py:227`, `:367`) — that is invariant 4 holding at the
  query layer and nothing here touches it.

## 9. Open questions

1. **Is the producer's measurement worth a wire-format change at all?**
   The honest alternative is that it belongs in the pack's README or the
   publisher's page, and the manifest stays a description of data. That
   position is defensible and cheaper.
2. **Should the hint be reproducible rather than merely signed?**
   `pack-format.md` §16 prefers *reproduction* over votes. There is no
   obvious cheap reproduction of a retrieval-quality claim — it needs the
   eval set, which may not be redistributable (JuriFindIT's licence is
   itself unverified, `gazzetta-plugin.md` §7.2).
3. **Does the reranker deserve any scope below the node**, given that
   `RERANK_URL` is a single env var and every federated query collapses to
   one scorer anyway?
4. **Is `rerank_class` (§7) worth having before a second non-text plugin
   exists?** Today it would guard exactly one collection type.
