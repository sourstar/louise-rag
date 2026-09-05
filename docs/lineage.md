# Lineage: how a collection continues, splits and gets replaced

*Proposal, 2026-09-05. For review — nothing here is built. Depends on
`no-central-node.md` (Codex, content-addressed snapshots, HRW) and
`certified-collections.md`; amends `pack-format.md` §6.1, §7, §17 and
CLAUDE.md invariant 1 in one respect stated in §6. Proposed amendments
67–71.*

---

## 1. Two relationships and one replacement

A collection's Codex is immutable and content-addressed
(`no-central-node.md` §5.1). Every change to what a collection *is*
therefore produces a new Codex and a new `collection_id`. This document
says which changes are which, what each inherits, and how the community
picks one when several appear.

| | **daughter** | **sister** | **corrective sister** |
|---|---|---|---|
| what changed | the toolchain: origin format or address, licence of new items, a parser adapted to the outside world | the space: a different encoder and/or chunker over the same corpus | a defect in the parent, in the corpus or in the vectors |
| records up to the transition | inherited by hash, unchanged | inherited by hash, unchanged | **recomputed from the origin** |
| records after the transition | produced by the new toolchain | identical to the parent's (§4) | produced by the corrected toolchain |
| space | the same `space_id`; vectors and coverage inherited | new; coverage from zero | **new by construction**; coverage from zero |
| Codex field | `continues: <codex>`, `from_snapshot` | `derived_from: <codex>` | `derived_from` + `replaces: {codex, defect}` |
| who may publish it | anyone, for an open collection; the certifier, for a certified one | anyone | anyone who can reproduce the defect |

Taking a *part* of a collection — one slice, one kind — is none of
these. It is the same collection with fewer members (`pack-format.md`
§10) and creates nothing.

## 2. Identity: two levels

`collection_id = H(codex)` stays the exact identity: two peers with the
same id hold byte-for-byte the same description. But the operator's
requirement for a daughter is that the collection *continues*, and an
id that changes at every adaptation to the outside world does not read
as continuity.

So there are two levels. `lineage_id` is the `collection_id` of the root
Codex, inherited along `continues`. The catalogue groups by lineage; the
Library shows one collection with a history, not a list of unrelated
ids. Adopting a daughter, for a peer that holds the parent, costs
reading one Codex: the records and vectors are already on disk.

A sister starts a lineage of its own — a different space is a different
thing to search — and records `derived_from` so the shared corpus is
visible. A corrective sister does the same, and additionally marks the
parent's lineage as superseded (§5).

## 3. The daughter

**3.1 The transition is a snapshot boundary.** Records up to `N` belong
to the parent's toolchain, records from `N+1` to the daughter's. No
record is ever produced by both; determinism demands it. The licence
changes at the same point: `license.terms_sha256` is in the Codex, so
the daughter carries the new terms, the acknowledgement of amendment 46
covers the new shards, and the old shards stay under the old terms.

**3.2 Old shards may lose their origin.** If the origin moved or changed
format, shards before `N` may no longer verify with `records-vs-origin`:
the old address is dead, the old form is gone. They remain verifiable
internally (`roots`, `chunks`) and through seals where these exist
(`certified-collections.md` §6), and their trust level **freezes** at
what it had reached. The daughter must say so —
`origin_reachable_until: <snapshot>` — and carries the parent's
toolchain digest beside its own, so a verifier knows which parser
applies to which snapshot range. This is the same reason seal evidence
is captured at once: the origin does not wait.

**3.3 Same space, by declaration.** A daughter keeps the parent's
`space_epoch` (§6), hence its `space_id`, hence every vector. The new
parser must produce text in the same *kind* as before — same cleaning
version, same layers — or it is not a daughter but a sister in
disguise; `keep.codex.create(continues=…)` refuses a daughter whose
`space` block differs from the parent's.

## 4. The sister

The repo already has this as the *derived collection* of
`pack-format.md` §17: records and text index reused by hash, a new
space from `coverage: 0`. The genomic case is the motivating one — an
encoder trained for the domain, over a corpus that already exists.

Two things content-addressing adds:

**Shared future.** A sister's origin and toolchain are the parent's, so
its snapshot `N+1` is the parent's snapshot `N+1` — same bytes, same
hash — with no coordination. Peers holding either contribute to the
same corpus for as long as both live, and discover it from the hashes
coinciding. A sister that *also* changes toolchain (the operator's
"case 1 + 2") is a sister of a daughter and carries both fields; from
the transition on, its records diverge from the parent's and the shared
future ends, visibly.

**Sisters by intent.** Two collections that happen to share encoder,
layers, cleaning and encoder digest are in one space and can be
searched as one list — `test_retrieval_federation.py` exercises this.
Today that happens by hoping parameters match. `space_from: <codex>`
copies the parent's `space` block byte for byte, so the Gazzetta and
EUR-Lex land in one legal space by construction, and a norm reference
in one resolves in the other. The encoder image digest is part of what
is copied (§6): two "sisters" on different builds of the same model
look comparable and are not — this node's b10630 incident, as kinship.

**Calibration bootstrap.** Whoever creates a sister *produces* its
`calibration` block — sample chunks and reference vectors — on their own
hardware, and that becomes what every later contributor is gated
against. The first set is unverifiable by anyone; it is the same
creator-trust bootstrap the corpus already accepts.

## 5. The corrective sister

**5.1 Why from zero.** A parser fix that changes 3 % of records could,
in principle, be a daughter with a repair delta: re-issue the affected
shards, keep 97 % of the vectors. The hash comparison is exhaustive if
run over every record. It was still rejected, for two reasons. First,
the b10630 case: 8 % of vectors corrupt with the corpus intact,
invisible to any hash, separable from the good ones only by recomputing
all of them — the partial repair costs what the full one costs and adds
a second code path. Second, and decisive: a protocol whose product is
trust cannot offer a repair that is "complete enough". One path, from
zero. This is the price of integrity, and `embedding-redundancy.md` §6
already accepts it.

**5.2 The defect must be reproducible.** Nothing executable travels
(`pack-format.md` §15), but the platform already has what is needed to
*reproduce* a defect: the checks. A corrective sister carries data:

```
replaces: {
  codex: <hash of the defective collection>,
  defect: {
    check: "records-vs-origin" | "vectors-sample",
    toolchain: <image digest under which the check fails>,   -- the corrected parser, or a known-good encoder
    witnesses: [<shard or record ids>],
    expected: <hash | vector>, observed: <…>,
    rationale: "<one readable sentence: what was wrong and why>"
  }
}
```

A peer that receives a sister with `replaces` **reproduces the defect
before honouring the replacement**: it runs the named check on the
named witnesses against the old collection and observes the failure.
If it does not fail, this is an ordinary sister — legitimate, with no
right to mark anything superseded. If it fails, §16 applies as it
stands: a failing deterministic check is a proof and names the shard;
a statistical one (encoder) needs two independent reproductions. The
old collection becomes *flagged*; the sister is the remedy to a flag,
not an opinion about a healthy collection.

What this proves, and what it does not: **non-conformance**, in a way
anyone reproduces — the bytes differ, the vectors leave the tolerance.
*Which of the two is right* is the publisher's claim, and it is
reviewed where §15 says code is reviewed: in the pull request that
fixes the parser. The protocol guarantees that nobody can replace a
healthy collection by pretending a bug; it does not guarantee the fix
is good. That is the right place for the line.

**5.3 The replaced collection.**

| | |
|---|---|
| deleted | never — nothing published is deleted |
| catalogue | *flagged*, *superseded by <codex>* |
| search | still possible, with a warning |
| embedding | excluded from HRW eligibility: no GPU is spent on a superseded space |
| seeding | continues — it is the provenance of what was there |

## 6. `space_epoch`, and an invariant left as it is

With a parser-only defect, encoder and chunker identical, the corrective
sister would compute the *same* `space_id` as its parent, and every
coverage map would happily reuse the parent's vectors by chunk hash —
exactly what "from zero" forbids. The Codex therefore carries a
`space_epoch`, salted with `replaces.codex`, and `space_id` includes it:
a corrective sister has a fresh space even with identical parameters; a
daughter keeps the epoch and therefore the vectors. The publisher
declares which, with `continues` or `replaces`; the protocol makes it
irreversible. The encoder image digest joins `space_id` for the same
reason (§4).

A change to invariant 1 was considered and **withdrawn**: moving
`cleaning_version` out of `space_id` into the toolchain would have let
a cleaning change re-embed only the records whose cleaned text
changed. It is the same partial-repair path §5.1 rejects. `space_id`
stays *encoder + layers + cleaning + epoch*; a cleaning change is a
sister; the re-pin is the price.

## 7. Competing daughters: selection before GPU

For an open collection whose origin changes, two peers may publish two
daughters. Their Codexes propagate — kilobytes, harmless. What must not
propagate is GPU spend on both.

**7.1 Equivalent daughters need no contest.** If both parse the new
origin the same way, their snapshots `N+1` have the same hash and, with
the same `space_id`, the same vectors. A peer holding both sees the
hashes coincide and publishes an equivalence attestation — the same
attestation as toolchain equivalence in `no-central-node.md` §5.1. The
catalogue merges them under one lineage node. Waste: two Codex files.

**7.2 Non-equivalent daughters are decided at the records stage.** One
keeps footnotes, one drops them: different records, chunks, vectors.
Records are cheap (14 GB for PubMed, CPU or download); vectors are
everything (55 GB and GPU-days). Selection therefore happens before
anyone embeds. All of it is computed locally, as everything in §16 is —
no global vote:

| | |
|---|---|
| **adoption** | distinct peer keys announcing possession of D's *records* for `N+1` under `H(D ‖ N+1)`, weighted by the local trust list. Not attestations, not vectors: who has fetched and verified the corpus |
| **cooling window** | from the first daughter seen for a lineage, `keep.adopt` of records is allowed and HRW eligibility for embedding is **false for every daughter of that lineage**. Length: `grace_days`, already in the Codex — the daughter concerns one month's delta, and the cadence already has seven days of slack. No new parameter |
| **leader** | when the window closes, only the daughter with the highest adoption is eligible; ties break on the lower Codex hash — deterministic, nothing negotiated |
| **hysteresis** | the leader changes only if a rival exceeds it by a margin for *k* consecutive gossip intervals |
| **the others** | never deleted; *not leading* in the catalogue; excluded from HRW; still seeded and searchable by whoever holds them. Partial bundles already published remain the provenance of a losing branch |

Cost: `grace_days` of delay on embedding the new records when the origin
changes — rare, and one month's delta, not the corpus. Residual waste:
the losing daughters' records (cheap) and the rare partial bundle from
a peer whose leader was overtaken after hysteresis.

**7.3 Sybil, honestly.** Adoption counts can be inflated by minting
keys; trust-list weighting dampens this and does not remove it. The
worst case is bounded: the community embeds the attacker's daughter,
which is still a *valid* derivation from the real origin —
`records-vs-origin` passes. Not corruption; a parsing preference
imposed by a Sybil, and the community can fork again. An expensive
attack to keep footnotes in.

**7.4 Where selection really happens.** Parsers live in the platform
(§15) and are reviewed as code. Two daughters are two parser variants;
the next release picks one, and its image digest becomes the toolchain
new peers receive by default — shown in the catalogue as *platform
default*. Adoption is the fast signal that avoids waste in the interim;
code review is the slow one that decides. Certified collections have
none of this: one daughter, signed.

## 8. Proposed amendments

67. **`pack-format.md` §17** — "derived collection" is the **sister**;
    the section gains the **daughter** (`continues`, `from_snapshot`,
    same `space_epoch`, parent toolchain kept for the old range,
    `origin_reachable_until`) and the **corrective sister**
    (`replaces` with a reproducible `defect`). `lineage_id` is the
    root Codex's id along `continues`.
68. **`pack-format.md` §7** — "a parser change is a new snapshot, never
    a silent drift" is rewritten in three cases: equivalent → an
    attestation, same identity; not equivalent, forward only → a
    daughter; not equivalent over past records → a corrective sister
    from zero. A snapshot delta records origin changes only.
69. **`pack-format.md` §3 / CLAUDE.md invariant 1** — `space_id` gains
    `space_epoch` and the encoder image digest; `cleaning_version`
    stays. `space_from` copies a `space` block byte for byte.
70. **`pack-format.md` §16** — a `replaces.defect` must be reproduced by
    the receiving peer before the replaced collection is marked
    superseded; deterministic on one reproduction, statistical on two.
71. **`pack-format.md` §6.1, §11** — daughters of one lineage are
    ineligible for HRW during `grace_days` after the first is seen;
    thereafter only the adoption leader is eligible, ties on the lower
    Codex hash, with hysteresis.

## 9. Open questions

1. **Margin and *k* for hysteresis.** Too small oscillates, too large
   lets an early lead with a worse parser hold on. Start at 20 % and
   three intervals; measure on the first real daughter.
2. **Adoption weighting.** Trust-list weight as for attestations, or a
   flat count with a minimum age per key? The former is consistent, the
   latter is simpler to reason about under Sybil.
3. **Daughters of certified collections when the certifier is silent.**
   `succession: open-after` (`certified-collections.md` §2) turns the
   collection open; the first daughter published after that is an open
   daughter and §7 applies. Whether the certifier's later return can
   reclaim the lineage is not decided.
4. **Witness selection for `replaces.defect`.** A publisher chooses the
   witnesses; a peer reproduces exactly those. Should the peer also
   sample elsewhere, to guard against a defect that is real at the
   witnesses and immaterial everywhere else — a pretext for a
   replacement nobody needs?
5. **`origin_reachable_until` and trust freezing.** A frozen level
   should be shown as frozen ("verified as of 2026-08, origin since
   moved"), not as current. Wording for the Library.
