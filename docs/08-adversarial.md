# 8. Standing up to bad actors

There is no authority here that can stop anyone from publishing. In a
system whose whole promise is that a collection received from a stranger
can be checked against its origin (§3), the defence against a bad actor
cannot be a gate that refuses them at the door — there is no door, and no
doorkeeper. The defence is economic: publishing junk must earn no
advantage, and cost nearly nothing to ignore. Every countermeasure in
this document obeys one rule.

## 8.1 The governing rule: no countermeasure may require knowing whom to trust in advance

Any defence that needs a reputation weight either smuggles in a trust
bootstrap — some privileged set whose word counts first — or is circular:
attestations feed reputation, reputation feeds ranking, ranking decides
whose attestations count. A newcomer with an empty trust list would then
verify nothing, forever. So no mechanism below ranks anyone by who they
are or who vouches for them. The accepted cost is that a newcomer sees
little at first: visibility is earned by independent reproduction, not
granted by introduction. That is the property, not a defect — trust is
reproduction (§3.6).

Two tempting defences are rejected outright for breaking this rule, and
named here so they are not proposed again:

- **weighting attestations by the reader's trust list** — an honest
  newcomer's list is empty, so nothing is ever verified; filling it
  requires a privileged bootstrap set, which is a central authority in
  disguise;
- **counting adoption by trust-weighted votes** — circular as above, and
  a cartel of mutually-trusting real peers can hijack it while being
  harder to detect than a Sybil.

Per-sender rate limiting is also too weak on its own: minting a fresh key
is free for the attacker, while keeping per-sender state costs the
defender.

## 8.2 Three defences that need no reputation

**The name is never a criterion.** Identity is the Codex hash (§2.1);
kinship is the hash chain (§2.9). A collection with a confusingly similar
title inherits neither the lineage nor the attestations of the one it
imitates. `title` is an untrusted field: displayed, never used to sort,
merge or resolve. Two collections both called "PubMed" appear side by
side, each with its own verification facts, and the reader chooses on the
facts.

**A collection with no reproduced vectors is inert.** A new collection
starts at coverage zero and *unverified*. It attracts no embedding work
and no ranking until independent parties reproduce a sample of its
vectors on its own records (§3.7). Ignoring it costs one Codex read.
Visibility is a function of independent reproduction, not of how many
collections a key has published — so an avalanche of empty siblings
collapses into a single low-priority row.

**Against the avalanche, limit the aggregate, not the sender.** A peer
accepts and propagates only so much *new unverified* material per unit
time in total, regardless of who sends it; beyond that, new material goes
to a slow, low-priority queue. A thousand fresh keys fill that queue no
faster than one, because the scarce resource being protected is
unverified novelty, not identity.

## 8.3 Two thresholds, because calibration proves the encoder, not the work

Calibration blocks (§3.4) are tied to an encoder and are *meant* to be
shared across collections: two siblings on the same encoder share one
calibration. So passing the calibration gate proves only that a peer can
run the declared encoder — not that any work was done on this collection.
This forces two distinct levels:

- **unverified** — signature plus calibration. Near-zero cost. It buys
  existence and encoder conformity, and says exactly that: "encoder
  verified, content not yet reproduced."
- **checked** — independent third parties resample `vectors-sample`,
  which are vectors on the collection's *own records* and so cannot be
  forged by copying a shared calibration block. This proves the work
  exists.

The distance between the two is the whole point. Anything a lone actor
can produce cheaply lands at unverified and stays there until others,
spending their own GPUs on their own initiative, move it to checked. This
is where a bad actor is actually stopped: not at entry, but at the point
where visibility must be earned from strangers who owe them nothing.

## 8.4 The maliciously-valid sibling is out of scope, on purpose

A sibling (§2.9) that pays real embedding work, passes the ladder (§3.6)
and is reproduced by independent verifiers *is a good collection*,
whatever the intent behind it. The difference between a welcome sibling
and a spiteful one is semantic and lives in the eye of the chooser.
Defending against it would require the protocol to judge merit — the one
central authority everything else here refuses. It is left to human
review of encoders and parsers, and to each peer's own trust list. The
protocol's job ends at making the claim checkable; deciding whether a
checkable collection deserves attention is the reader's.

## 8.5 The work parameter: declared by the collection, not imposed by the protocol

Proposing a collection carries a stated amount of real embedding work.
Earlier this was conceived as a universal entry threshold measured
against a reference machine — a reference hour on a fixed GPU. That is
abandoned, for a decisive reason: any universal hardware reference is a
cage. It requires whoever proposes a collection to own or reach that
exact machine, it dies when the machine is discontinued, and it forces an
unreliable model-to-model cost extrapolation for every encoder that is
not the reference one. Louise survives beyond the creator (§4.1); it must
also survive beyond any one piece of silicon.

So there is no reference machine. The **work parameter** is measured by
the creator, on the creator's own hardware, with the collection's own
encoder, and written into the Codex (§2.4) with no reference to the
hardware used. Every participant adopts that number as stated. Nothing is
converted between encoders; no one needs to possess a particular machine;
there is no extrapolation. A sibling (§2.9) that wants a heavier, more
capable encoder measures its own parameter and writes a higher one,
which by design excludes weaker hardware from contributing — and that is
the point of such a sibling. A datacentre may build siblings tuned to its
own scale, and that is welcome. An encoder that can only run on CPU
carries the parameter the creator measured on that CPU, which is exactly
the class of hardware that collection can use — so the CPU case needs no
special constant and no conversion table.

### What the parameter is, and is not

It is **not** a barrier against dishonest actors. Those are stopped by
§8.2 and by third-party reproduction (§8.3). A creator who writes a low
parameter to enter cheaply gains nothing, because a low number only
declares that this collection works at a low level, and it stays inert
until strangers choose to reproduce it — which no one does for a
collection that interests no one. A low parameter is not an exploit; it
is true information about a collection that does not matter.

It **is** a protection for honest creators and a descriptor of the
collection's purpose. It states the level a collection works at and the
weight of encoder it uses, and so it tells a would-be contributor who it
makes sense to count among the participants in the embedding — which
depends on what the collection is for. Different collections declare
different bands because they should: each declares its own, rather than
being held to one metre for all.

### Why a declared number cannot be inflated

The parameter is stated by the creator but not believed on their word. It
is expressed in tokens of the declared encoder's tokenizer (§2.4), and
those tokens correspond to vectors that are reproducible on the
collection's own records. A third party runs the declared encoder,
resamples the vectors, and checks that the declared work actually exists.
The creator fixes the parameter; the creator cannot lie about the
quantity of work produced, because that quantity lives in the vectors,
not in the declaration. Hardware disappears from the reference;
reproducibility of the vectors remains the anchor.

## 8.6 Continuity: a rate, not a toll, and "someone", not the creator

Whatever level a collection declares, holding it is a sustained activity,
not a single payment. A one-off toll stops the lazy but not the
well-resourced, who would pay once, enter, and abandon the collection to
the peers — the very cost-dumping to prevent. So a live collection is one
where signed blocks keep being produced at its declared cadence; a decoy
or an abandoned collection decays by itself from *actively-held* to
*dormant* — still findable, no longer listed as active — with no
judgement by anyone.

The signal is "someone continues", never "the creator continues". Tying
vitality to the creator would contradict the single-survivor requirement
(§4.1). There is no privileged creator role: the creator is simply the
first key to stand behind the collection, and any other key doing the
same work enters the same class. A collection is alive while at least one
such key is still contributing; when none is, it decays, and anyone may
revive it. No history of who began it is kept — only the changing set of
who currently holds it.
