# 8. Standing up to bad actors

*(vocabulary amended 2026-10-08: SHARD, parts)*

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

A trust list still exists (`hold key trust`, §7.3). It is a local
allow/deny filter over whose attestations a peer counts at all, and a
peer's own checks set its own level whatever the list says (§3.6). It is
never a weight, and adoption never consults it (§4.6).

Per-sender rate limiting is also too weak on its own: minting a fresh key
is free for the attacker, while keeping per-sender state costs the
defender.

## 8.2 Three defences that need no reputation

**The name is never a criterion.** Identity is the Codex hash (§2.1);
lineage is the hash chain (§2.7). A collection with a confusingly similar
title inherits neither the lineage nor the attestations of the one it
imitates. `title` sits in the Codex's `descriptive` block, the one
block the identity does not cover (§2.1) — so two names over one recipe
are one collection, and a name buys an impostor nothing at all. It is
an untrusted field:
displayed, never used to sort, merge or resolve. Kinship is the chain of
`continues`, `derived_from` and `replaces` (§2.7); nothing else makes two
collections kin, a shared title least of all. Two collections both called
"PubMed" appear side by side, each with its own verification facts, and
the reader chooses on the facts.

**A collection with no reproduced vectors is inert.** A new collection
starts at coverage zero and *unverified*: usable and searched by whoever
holds it, marked as such wherever it appears (§3.6), and nothing more.
It earns no place in catalogue rankings until independent parties
reproduce a sample of its vectors on its own records (§3.5–3.6). It earns
no stranger's GPU either, though the precondition there is on the
records, not on the vectors: a peer contributes vectors to a collection
only after its own `roots` and `chunks` pass on the parts it holds and
at least one `records-vs-origin` attestation — its own or another key's —
exists for the snapshot (§4.5). Records are checked before GPU is spent;
a vector precondition would be circular, because the first vectors on any
collection are someone's unreproduced vectors. Ignoring it costs one
Codex read. Visibility is a function of independent reproduction, not of
how many collections a key has published — so an avalanche of empty
sisters collapses into a single low-priority row.

**Against the avalanche, limit the aggregate, not the sender.** A peer
accepts and propagates only so much *new unverified* material per unit
time in total, regardless of who sends it; beyond that, new material goes
to a slow, low-priority queue. Unverified material is Codexes,
snapshots, attestations
and bundles arriving from other peers and signed by keys none of whose
work this peer has seen reproduced — no bundle of theirs accepted through
the ladder (§3.5), no attestation of theirs this peer re-ran and
confirmed. Two allowances, because a flood can be large in bytes or
merely numerous: **64 MB and one thousand objects per hour**, whichever
binds first — an attestation is a few hundred bytes, so a million of
them fit inside any byte budget and would swamp a catalogue by count
alone. What is admitted enters the catalogue and is forwarded; the rest
waits in arrival order and is neither forwarded nor announced by this
peer until admitted. Material from a key whose work this peer has seen
reproduced is never queued. A thousand fresh keys fill that queue no
faster than one, because the scarce resource being protected is
unverified novelty, not identity.

**The numbers are the peer's, not the protocol's.** Both allowances and
the queue's depth are set in `platform.toml` (07 §7.8); the figures
above are the defaults, chosen so that a household connection admits a
new collection and its first snapshots within the hour while a flood
takes days. A peer on a fast line raises them; a peer on a metered one
lowers them; a peer that lowers them to zero accepts nothing from
strangers and is complete in every other respect. Nothing in the
protocol reads another peer's setting, and no peer can tell what
another's is: the limit governs what *this* peer accepts, so unlike a
threshold that had to be agreed it cannot drift, cannot be negotiated,
and cannot be gamed by anyone but its owner.

**The queue is bounded, and drops the newest.** When it is full the
arriving object is refused rather than an older one evicted; otherwise
a flooder empties the queue of legitimate material simply by arriving
later, which is the cheapest attack there is. A refusal is not a
judgement about the sender and is not remembered: the object is
announced again by whoever holds it, and is admitted when there is
room.

## 8.3 Two thresholds, because calibration proves the encoder, not the work

The calibration set (§2.4; the gate is §3.3) is tied to an encoder, not
to a corpus, and is *meant* to be shared across collections: two sisters
on the same encoder share one calibration. So passing the calibration
gate proves only that a peer can run the declared encoder — not that any
work was done on this collection. This forces two distinct levels, and
they are the first two of §3.6's table; the thresholds are §3.6's, and
this document defines none of its own:

- **unverified** — the publisher's word: a signed snapshot, signed
  bundles, a calibration pass. Near-zero cost, everything a lone key can
  produce for itself. It buys existence and encoder conformity, and says
  exactly that: "encoder verified, content not yet reproduced."
- **checked** — the counts of §3.6 are met, among them that a key other
  than the producer has passed `vectors-sample` on the producer's
  vectors. Those are vectors on the collection's *own records* and so
  cannot be forged by copying a shared calibration set. This proves the
  work exists.

The distance between the two is the whole point. Anything a lone actor
can produce cheaply lands at unverified and stays there until others,
spending their own GPUs on their own initiative, move it to checked. This
is where a bad actor is actually stopped: not at entry, but at the point
where visibility must be earned from strangers who owe them nothing.
Keys are free, so the counts in §3.6 are advisory: what protects a peer
is its own checks and the aggregate limit of §8.2, and the levels say how
many strangers have already done what the peer could do itself.

## 8.4 The maliciously-valid sister is out of scope, on purpose

A sister (§2.7) that pays real embedding work, passes the ladder (§3.5)
and is reproduced by independent verifiers *is a good collection*,
whatever the intent behind it. The difference between a welcome sister
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
encoder, and written into the Codex with no reference to the hardware
used. The field is `work: {tokens, per}` (§2.1): a rate — `tokens` of the
declared encoder's tokenizer per `per`, an ISO 8601 duration that
defaults to the update cadence — and what the rate means is §2.4. Every
participant adopts that number as stated. Nothing is converted between
encoders; no one needs to possess a particular machine; there is no
extrapolation. A sister (§2.7) that wants a heavier, more capable encoder
measures its own parameter and writes a higher one, which by design
excludes weaker hardware from contributing — and that is the point of
such a sister. A datacentre may build sisters tuned to its own scale, and
that is welcome. An encoder that can only run on CPU carries the
parameter the creator measured on that CPU, which is exactly the class of
hardware that collection can use — so the CPU case needs no special
constant and no conversion table.

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

The one block serves twice. For **holding** (§8.6; the computable rule is
§4.7): a key holds the collection actively while its signed work on the
collection's own records — bundles, and attestations of checks — totals
at least `tokens` per `per`. For
**eligibility** (§4.5): a peer is eligible for a part when
`3 × part_tokens / benchmark_tokens_per_s ≤ seconds(per)` — a
contributor must be able to finish one part within one work period, with
a factor of three for other load. `part_tokens` is listed per part in
the snapshot, an upper bound of chunks × `ctx`; the benchmark is the
peer's own (§3.3). Small parts admit weak peers, large parts admit few,
and the rate says who it makes sense to count — the paragraph above, made
computable.

### Why a declared number cannot be inflated

The parameter is stated by the creator but not believed on their word. It
is expressed in tokens of the declared encoder's tokenizer (§2.4), and
those tokens correspond to vectors that are reproducible on the
collection's own records. A third party runs the declared encoder,
resamples the vectors, and checks that the declared work actually exists:
the check is `vectors-sample` on the creator's own bundles (§3.2), and
there is no other. The creator fixes the parameter; the creator cannot
lie about the quantity of work produced, because that quantity lives in
the vectors, not in the declaration. A declared rate nobody meets makes
the collection dormant (§8.6), which is true information. Hardware
disappears from the reference; reproducibility of the vectors remains the
anchor.

## 8.6 Continuity: a rate, not a toll, and "someone", not the creator

Whatever level a collection declares, holding it is a sustained activity,
not a single payment. A one-off toll stops the lazy but not the
well-resourced, who would pay once, enter, and abandon the collection to
the peers — the very cost-dumping to prevent. So a live collection is one
where **signed blocks** — batches of vectors on the collection's own
records, each signed by the key that produced them, delivered whole at
the collection's declared granularity — keep being produced at the
cadence the work parameter states. A collection whose blocks keep
arriving is **actively-held**; one whose blocks have stopped is
**dormant**: still findable and fetchable, no longer listed as active. A
decoy or an abandoned collection thus decays by itself. The two states
are observed, not declared, and carry no judgement by anyone.

A signed block is what §3.5 and §4.5 call a **bundle** — one thing, two
names, and §1's table lists both. The computable rule is §4.7: on a
peer's catalogue a collection is actively-held while some key's signed
work for it, seen by that peer, totals at least `work.tokens` per
`work.per`; otherwise it is dormant. Signed work is a bundle where there
is embedding to do and an **attestation** where there is not, because a
finished collection is kept alive by being checked, and one nobody
checks any more is decaying however complete it looks. Each peer
computes the state from the work it has seen — its coverage map for
bundles (§2.5), the attestations it holds for checks — and shows it beside
the trust level; a dormant collection leaves the default listing and
nothing else changes — it is still served, fetched and searched, and the
next signed work that meets the rate makes it active again. A collection
with nothing left to embed is not dormant for lack of it: the checks
its holders keep running are the work that keeps it alive (§4.7).

The signal is "someone continues", never "the creator continues". Tying
vitality to the creator would contradict the single-survivor requirement
(§4.1). There is no privileged creator role: the creator is simply the
first key to stand behind the collection, and any other key doing the
same work enters the same class. A collection is alive while at least one
such key is still contributing; when none is, it decays, and anyone may
revive it. No history of who began it is used — *first published by* is
recorded in catalogues as a fact (§2.1) and decides nothing; what counts
is the changing set of who currently holds it.

## 8.7 Figures: the one thing here a viewer renders

Everything else a peer accepts is consumed by a program — text is chunked,
vectors are multiplied, an index is queried. A **figure** (09 §9.5) is
different in kind: it is attacker-supplied bytes that end up in front of a
person, through an image decoder the platform does not write. That is a
larger attack surface than the rest of the exchange put together, and the
hash chain does not touch it.

**What the hash does and does not buy.** A record names its plates by
hash, the record line is in a part whose root a snapshot signs, so a
plate SWAPPED IN TRANSIT is caught. Nothing about that catches a plate the
producer meant to send. A figure is published, not merely relayed, and a
hostile publisher's plate verifies perfectly. So the defences below are
about what a well-formed figure may be, and none of them is a signature.

**The admitted types are a closed list**: `image/jpeg` and `image/png`.
Two, because they are the two the measured corpora hold, and a format
admitted "in case" is a decoder admitted in case. `media_type` on the
record line is compared against what the bytes ACTUALLY ARE — the magic
number and a structural parse — and a disagreement refuses the figure,
never the reverse. Extending the list is an amendment to this section and
a new `record_line` version, because a peer that cannot decode a type
cannot show the document.

**SVG is refused, and is the reason the list is closed rather than open.**
An SVG is a document, not an image: it carries script, external
references and its own fetches. There is no safe subset worth the cost of
policing one, and "we strip the script tags" is the sentence that precedes
every such failure.

**Caps, applied before decoding.** At most 16 MiB of bytes per figure, at
most 64 megapixels once the header is read but before any pixel is, and
at most 4,096 figures per record. The first two are what stop a
decompression bomb — a few hundred kilobytes of PNG that decodes to
tens of gigabytes — and they are applied to the DECLARED dimensions in
the header, so the decoder is never handed something it will die inside.
The third stops a record from being a container in its own right.

These three numbers were the section's weakest part until 2026-09-09,
when they were measured against 44,477 illustrated epubs holding 786,699
images. Two held and one did not.

The byte cap is generous and stays: the largest plate in that corpus is
1.8 MB and the 99.9th percentile is under 1 MB, so 16 MiB refuses
nothing real. It bounds ATTACKER bytes rather than honest ones, and
tightening it against one corpus would be unwise when patent drawings
and maps are the cases still to come.

The count cap was 1,024 and refused eleven books, the largest carrying
3,503 images — which is 0.025 % of the corpus and the WRONG 0.025 %:
they are the most illustrated books in it, which is what a plates
collection is for. A rule that admits a novel's frontispiece and refuses
an illustrated natural history is backwards, so the cap is 4,096.

The media-type list was measured too: `image/jpeg` and `image/png` cover
99.66 % of that corpus. SVG is REAL in it — 2,407 images — so refusing
it is a live cost and not a hypothetical one, and the cost was counted:
ten books lose at least one plate and NO BOOK loses all of them. A
record whose image is refused still names it, with `carried: false`
(09 §9.5), so a reader meeting the caption is told the picture is gone
rather than left to hunt for it.

**A figure is never executed, never fetched from, and never resolved.**
Whatever a format permits, a peer that stores or serves a figure treats
it as opaque bytes; only a peer that DISPLAYS one decodes it, and it
decodes with the caps above. An embedded reference in a figure is not
followed. This is 01's "never anything executable", which figures bind
harder than any other member.

**What is out of scope, and stated so nobody assumes otherwise.** A plate
whose hash is right, whose type is right and whose bytes decode, but
which is not the picture the caption describes, is not detectable here —
a correct swap made upstream of the parser is a corpus-fidelity question
(invariant 11) answered by `records-vs-origin`, which re-fetches from the
origin and reproduces. And a figure carries whatever its own metadata
says: EXIF is not stripped, because stripping it would change the bytes
and so the hash, and a figure that does not match its record is worse
than one carrying a camera model.
