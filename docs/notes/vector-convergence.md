# Converging on one version of the vectors

*Proposal, operator's requirement 2026-10-19. Not yet in the specification;
it amends 02 §2.4–2.5 and 04 §4.5 once reviewed. Nothing here is new
machinery: it is 04 §4.6's adoption rule and a CRDT-style total order,
applied to vectors.*

## 1. The gap

02 §2.4: "Two peers that accepted different producers hold different rows
for the same part, each within tolerance; the coverage map says which."
02 §2.5: the coverage map is "the only part that is allowed to differ
between two peers **for a while**." No rule ends the while.

Rendezvous (04 §4.5) keeps duplicates rare on a connected network, but
two networks that cannot see each other each run their own rendezvous,
embed the same parts and seed their bundles. When they reconnect, every
peer keeps whichever version reached it first: the network serves two
versions of the same chunks indefinitely, and every later
`vectors-sample` compares against whichever one the checker happens to
hold. The same happens with equivalent SETS of vectors at any size — a
genesis archive and a community embedding of the same records, two
engines' complete embeddings of a part, a carried space beside a
re-embed.

## 2. The unit

A **version** is a set of vectors for a chunk range in one space, under
one producer: one bundle, every bundle of one producer for a part, a
genesis archive's rows, or a whole collection's vectors. Versions are
compared on the chunks they share; a version is named by the ids of the
signed artefacts it consists of (bundle ids, an archive's digest), so
every peer names it the same way.

## 3. Equivalence — the existing check

Two versions over the same chunks are **equivalent** when every compared
chunk agrees within the space's `tolerance` (03 §3.1) — exactly the
comparison "duplicates are checks" already makes and publishes as a
`vectors-sample` attestation naming both producers (04 §4.5). Each
version must also come from a run that passed the calibration gate
(03 §3.3) and from an engine meeting invariant 12. Two versions that are
NOT equivalent are not a convergence question: one producer is wrong,
and 03 §3.6's flag procedure applies.

The operator's cross-device review line (approved-encoders.md: below
0.995 against a CPU twin) is about admitting an engine, not about
choosing between two admitted versions; it does not enter here.

## 4. The survivor — 04 §4.6's adoption, applied to vectors

Among equivalent versions of the same chunks, every peer applies the
same total order, so the network converges on one:

1. **Checks:** the number of distinct keys whose passing `vectors-sample`
   attestations cover the version — a better-checked version wins
   (review, 2026-10-19).
2. **Adoption:** the number of distinct peer keys that have accepted the
   version and list it in their digests, a key counted only once its
   listing is at least one digest interval old — 04 §4.6's rule for
   daughters, unchanged. Not age: age says nothing about checks.
3. **Hysteresis:** the survivor changes only when a rival exceeds it by
   twenty percent for three consecutive digest intervals, as this peer
   observes them (04 §4.6), so a choice does not flap while news spreads.
4. **Tie:** see §7 — an input the producer cannot choose.

The order is a deterministic function of signed, content-addressed
inputs and of digests every peer receives; two peers that have seen the
same digests choose the same survivor, which is the convergence property
of a last-writer-wins register with a total order, with "checks, then
adoption" in place of a timestamp.

**Scope.** Two versions from the same run are byte-identical (invariant
12) and never compete; the rule is for different runs and engines, each
admitted by the gate. The operator's 0.995 CPU-twin line admits an
ENGINE and plays no part in choosing between two admitted versions.

## 5. What a peer does with a losing version — lazily

RULE 1: equivalent rows serve search identically, so **no peer rewrites
rows only to converge**. The survivor decides what a peer SEEDS and
ANNOUNCES for the part:

- A peer holding the loser keeps its rows, stops seeding the loser, and
  seeds the survivor once it holds it; it fetches the survivor's bundles
  only when it would move that part anyway — a re-share, a new peer
  asking, a rewrite of the part for another reason.
- When rows are swapped, it is through the ladder (03 §3.5), one bundle
  at a time, delete-then-insert (invariant 2), every byte drawn from the
  transfer budget (invariant 5) and counted as maintenance with its own
  scheduling numbers (scheduling.md §6).
- The loser is kept as what it already produced: the `vectors-sample`
  attestation naming both.
- If the survivor is later flagged (03 §3.6), the next version in the
  order takes its place — 04 §4.5's existing replacement rule.
- Nothing published is deleted (docs/01): a losing version is no longer
  served by peers that converged, but a peer may keep it.

## 6. Where it runs

The worker, beside rendezvous: the same code that will make the embed
stage honour 04 §4.5 (STATUS: the worker's `embed = "all"` ignores the
ordering; the fix is queued after pubmed-v8, 2026-10-20) reads the
coverage map and the digests and applies §4 per part, once per digest
interval — a cost that scales with the parts that HAVE competing
versions, never with the corpus (CLAUDE.md engineering rules).

## 7. Open

- **The tie-break must be an input the producer cannot choose.** The
  lowest bundle id is grindable: a producer controls its signed bytes and
  can re-sign until its id is low. Any hash over producer-controlled bytes
  stays grindable, and 09 §9.1's sampling seed is chosen by each verifier,
  not public randomness. Candidate: the lowest `H(bundle_id ‖ hash of the
  collection's first snapshot published after the bundle)` — unknown when
  the bundle is made, the same for every peer, at the cost of settling a
  tie only at the next snapshot. The stake is bounded either way: a tie
  only chooses between EQUIVALENT versions, so gaming it buys nothing but
  having one's own equivalent bytes served.

- A version's adoption is only as good as the digests: minted keys
  inflate it, as 04 §4.6 says for daughters; the bound is the same (the
  one-interval age, 08 §8.2's aggregate limit), and the worst case is
  that the network converges on an equivalent version — still within
  tolerance of every other.
- The wire: digests already list accepted bundles per part (`coverage`,
  09 §9.9); no new message is needed. Whether the digest should name a
  part's SURVIVOR explicitly, to make convergence observable, is a
  format question for 09.
- Granularity of the swap for very large versions (a genesis archive):
  per part, in the worker's existing archive passes (scheduling.md).
