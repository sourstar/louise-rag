# Review: converging on one version of the vectors

*2026-10-19, of [vector-convergence.md](vector-convergence.md) as of 14a153e.
The input to the operator's dedicated review of the whole peer-to-peer
architecture ("after your evaluation"). Nothing here is decided.*

## 1. What is actually at stake

Not correctness. Two admitted versions of a part are each within tolerance
of the space's reference; search over either serves the same function, and
every `vectors-sample` a checker runs re-embeds against ITS OWN engine, never
against another version (03 §3.2), so a check stays valid whichever version
the checker holds. What non-convergence costs is:

- **the swarm split** — a newcomer finds two sets of bundles for one part,
  and each set has half the seeders (04 §4.3's availability per part);
- **duplicate fetches** — a peer that receives the second version fetches
  and compares it; 04 §4.5 already calls that "the verification budget, paid
  by accident", up to the tenth of parts §3.6 wants re-embedded anyway;
- **observability** — two peers answer one query with slightly different
  scores, and nobody can say which version "the network" holds.

None of these has been measured. With three peers today (the NAS, the dev
PC, the laptop) the problem is a forecast. CLAUDE.md: measure before
optimising — the digests already carry the accepted bundles per part
(`coverage`, 09 §9.9), so the count of parts with more than one announced
version is one query over the digests a peer holds. That number is the
first thing the review should see.

## 2. The proposal, point by point

**Lazy (§5) — right.** RULE 1: equivalent rows are not rewritten to converge;
the survivor decides what is seeded and announced, rows move only when the
part moves anyway. This keeps 02 §2.4's rule ("the first accepted is the
row") untouched, which is a feature: the change is to seeding, not storage.

**"Stops seeding the loser" (§5) — a flaw.** The survivor is ranked by
checks first; the better-checked version can be the RARER one. A peer that
holds only the loser and stops seeding it seeds nothing for that part until
it fetches the survivor — which it does only "when it would move that part
anyway". If most peers hold the loser, the part's availability drops,
against 04 §4.1 ("one peer is enough"). Fix: a peer seeds what it holds
until it holds the survivor; it ANNOUNCES the survivor as the part's version
(so newcomers fetch it) and keeps the loser available meanwhile.

**Equivalence (§3) — tighten.** "Every compared chunk agrees within the
space's tolerance" is a pairwise comparison only a peer holding both
versions can make. A peer holding one relies on someone's attestation. And
tolerance against a common reference is not transitive: A within τ of the
reference and B within τ of it puts A and B only within roughly 2(1-τ) of
each other. Define equivalence as: both admitted (ladder passed), AND a
published pairwise `vectors-sample` naming both (04 §4.5's duplicate
attestation) at the space's tolerance. Without that attestation two
versions are "both admitted", not equivalent, and nothing converges.

**The order (§4).**
- *Checks first:* sound as a preference, but no more robust than adoption.
  A passing attestation proves the key signed it, not that it re-embedded
  anything; minted keys inflate both counts alike. The stake is bounded
  (only equivalent versions compete), so this may be acceptable — say so in
  the rule, as 04 §4.6 does for daughters.
- *An archive's version should rank first*, before checks: it is fetched
  from the origin by anyone (no peer has to seed it), it cannot be inflated
  by minted keys, and its rows are reproducible by the same-function proof.
  Under equivalence only: tonight's pubmed switch showed archive rows that
  are NOT the space's function (54,548 POPLINE rows, an abstract NCBI
  embedded and the record does not cut to; docs/notes/pubmed-other-abstracts.md)
  — those fail the pairwise comparison and are never candidates.
- *Hysteresis* (twenty percent, three intervals): borrowed from 04 §4.6
  where a wrong choice wastes GPU; here a wrong choice costs nothing, so a
  simpler rule may do. Keep it only if flapping is measured.

**The tie-break (§7).** `H(bundle_id ‖ next snapshot's hash)` is the right
idea — an input no producer controls at signing time. Two precisions:
- "the first snapshot published after the bundle" depends on who saw what
  when; use **the snapshot that follows the bundle's own `snapshot` in the
  collection's chain** (09 §9.4: consecutive ids, each naming its
  predecessor) — the same for every peer;
- a fetcher who also embeds can shape the next snapshot's bytes only
  within what the origin and the parser fix; for a frozen collection there
  is no next snapshot, so a frozen collection needs another input (its
  `collection_id`, say, with the grinding risk bounded as stated).

**Units (§2).** Mixing a bundle, a producer's bundles for a part, an
archive and a whole collection as "versions" makes comparison ambiguous.
The coverage map, the seeding and rendezvous are all per PART; so should
the survivor be. A whole-collection version is the union of its parts'.
A carried space is never a competitor: another space_id is another space.

**Where it runs (§6).** Beside the rendezvous fix (the embed stage honouring
04 §4.5), per part with competing versions, per digest interval — the cost
rule is right. But on a CONNECTED network rendezvous removes most duplicates
before they exist; convergence is mostly about networks that were apart and
rejoin. That ordering argues for fixing rendezvous first and measuring what
is left.

## 3. Alternatives for the review

- **(A) Accept multiple versions.** Cheapest. Costs: the split swarm and
  duplicate fetches, which §4.5 already budgets as verification.
- **(B) Prefer the most-seeded version when fetching.** Local, no new rule:
  a newcomer that sees two announced versions of a part fetches the one more
  peers announce; preferential attachment converges without a global order.
  No determinism, no observability.
- **(C) The proposal, amended as above.** Deterministic, observable, lazy;
  the cost is a rule in 02 §2.4/§2.5 and 04 §4.5 and its tests.

**Recommendation:** fix rendezvous (queued), then measure the parts with
more than one version across live peers. If they are few, (A) or (B). If
the network is meant to rejoin after partitions as a matter of course,
(C), with the archive first, the seeding fix, pairwise equivalence and the
chain-ordered tie-break.

## 4. Questions the architecture review should take with it

- Is a part's version part of what a peer PROMISES (then it belongs in the
  digest, 09 §9.9), or only a local seeding preference?
- Should a genesis archive's rows ever be seeded by peers at all, or always
  fetched from the origin? (Today: each peer imports from the archive.)
- The trust list is a local filter, never a weight (04 §4.6). Should a
  convergence count honour it like every other count?
- Snapshots supersede; vectors are per snapshot's parts. When a part is
  re-issued, its versions restart — does convergence carry across
  snapshots for unchanged records (A1c's reconcile does, locally)?
