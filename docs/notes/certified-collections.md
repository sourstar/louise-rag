# Certified collections: when one key is the point

> **Superseded** (2026-10-25, handover) by `docs/02-collections.md` §2.8 and `docs/03-checking.md` §3.7, which adopted it.


*Proposal, 2026-09-05. For review — nothing here is built. Companion to
`no-central-node.md`: that document removes every required authority
from open collections; this one reintroduces exactly one, on purpose,
and says what it may and may not do. Amends `pack-format.md` §7, §10,
§13, §15, §16; proposed amendments 62–66 (61 is the toolchain
hash, in `no-central-node.md`). Facts about the Gazzetta
Ufficiale and the EU trusted lists are from the publishers' own pages,
checked 2026-09-05.*

---

## 1. Two kinds of collection, not four

A collection is **open** or **certified**. Nothing else.

- **Open** — anyone may read, anyone may publish a snapshot by applying
  the Codex, anyone may embed. Trust is reproduction (`pack-format.md`
  §16). This is `no-central-node.md` in full.
- **Certified** — anyone may read; only the **certifier** may publish a
  snapshot that peers treat as certified. Embedding is open by default
  (§3). Trust is reproduction *plus* an anchored signature.

"Private" collections — read access restricted to members — were
considered and dropped. In a peer-to-peer system restriction means
encryption of records *and vectors* (vectors leak text under inversion),
a membership authority with its own succession problem, non-enumerable
rendezvous keys, and per-collection trust thresholds because five
members never reach *verified*. None of it exists in the platform and
none of it is wanted. The license gates (yellow/red classes) already
cover "not redistributable", which is the actual need.

What certification is for: law. The Gazzetta Ufficiale and the Official
Journal of the EU are authentic in their electronic form and carry the
publisher's seal. A search over them is only worth having if the text it
returns is *that* text, and if the fact can be checked by someone who
distrusts everyone involved in building the collection.

## 2. What the certifier is, and is not, allowed to do

A certified Codex adds one block and one rule:

```
publication: "certified",
certifier: {key: <ed25519>, anchor: <url>, is_origin: false},
embedding: "open",                 -- or "certifier-only"
succession: "frozen",              -- or {"open-after": "P6M"}
content: "manifest-only"           -- fixed, not configurable
```

The certifier **publishes record snapshots** — fetches the origin,
applies the Codex, signs. That is the whole of its authority. It does
not choose the encoder (the Codex does, at creation, once); it does not
exempt the collection from any check (§5); it does not own the vectors
unless the Codex says `certifier-only`. Certification adds a signature.
It never removes a check.

The Codex is **always public**, data or no data. A certifier who keeps
its parser or settings secret has published something nobody can
validate beyond hashes and a signature, and Louise should not list it.

## 3. Embedding: three levels, one mechanism

The certifier signs text. Vectors are derived from text by a fixed,
open encoder within a declared tolerance, and anyone holding the records
can check them. Requiring the certifier to compute them would put
GPU-hours on an authority for no gain in trust — and, as the operator
observed, is improbable until this becomes a standard elsewhere.

What makes open embedding acceptable on legal text: **a bad vector
cannot alter the text returned.** Vectors decide which records are found
and in what order; the reader always shows the record, verified by hash
against the certified snapshot. Embedding failure is a recall problem;
corpus failure is an authenticity problem. They are different risks,
and only the second is what certification exists to remove.

| level | who embeds | who vouches | certifier's cost |
|---|---|---|---|
| certified | the certifier | its signature on the bundles | GPU-hours over the corpus — improbable |
| **endorsed** | any peer | the certifier *as verifier*: runs `vectors-sample` (at high coverage if it wants) and signs the attestation | GPU-seconds; sampled |
| open | any peer | independent verifiers' attestations, the local trust list | none |

The middle level costs no new mechanism: attestations are already signed
events from any verifier (§16), and an attestation signed by the
certifier's key carries the certifier's weight. It also answers "which
of two compliant vector sets is the reference": the one the certifier
attested. `embedding: open` is the default for certified collections;
`certifier-only` stays available for the case that never happens.

## 4. Verifying the origin of a packet — the chain

"Verify where this packet came from" is a chain of five links. The pack
already has the inner three; the outer two are where the risk is.

```
external anchor → certifier key → signed snapshot → shard merkle root → bytes
```

**Bytes → root → signed snapshot.** Built. Torrent v2 verifies pieces
against roots on download; the snapshot lists the roots; the snapshot
is signed. A packet inherits its signature from the snapshot by hash and
needs none of its own.

**Snapshot → key.** Built, but it proves only "whoever holds this key
signed this". It does not prove the key is the Gazzetta's.

**Key → anchor.** Missing, and the only link Louise cannot supply
alone. It can only make checking it mandatory. The certifier publishes
its key fingerprint (or the Codex hash) under a domain it controls — a
well-known URL such as `https://<certifier domain>/.well-known/louise/
certifier.json`, optionally mirrored in a DNS TXT record — and the TLS
+ DNSSEC chain of that domain is the anchor. The Codex carries the
anchor URL; a peer fetches it at adoption and periodically thereafter.
Mismatch means the collection is *open, published by a key that claims
to be X* and is shown as such. **Rotation** (a new key signed by the
old) and **revocation** live at the same anchor; without revocation a
compromised key certifies forever.

For "which snapshot is current", the BEP 44 mutable item under the
certifier's key — dropped for open collections because a single writer
was a defect — is exactly right here, because the certifier *is* the
single legitimate writer. The anchor URL is the fallback when the DHT is
unreachable.

Two defences in depth, because an anchor is one link.

**4.1 Reproduction stays mandatory.** For a certified collection over a
public origin, `records-vs-origin` runs regardless of the signature. An
attacker then has to compromise both the key and the origin. A valid
signature over records that do not match the origin is the proof of
compromise, and it flags the snapshot even though the signature is
perfect.

**4.2 Where the origin seals its documents, keep and check the seal.**
The Gazzetta Ufficiale telematica has been the authentic edition since
2 January 2009; its certified PDF is distributed as `<date>_<issue>_
<sn>.pdf.p7m` — a CAdES envelope around the PDF — signed by the officer
responsible for the publication, with a per-page digital stamp from the
IPZS. The OJ of the EU carries a qualified seal. Verification is
machine-only and needs no trust in any website: the signer's certificate
chains to a trust anchor in the national trusted list; the European
Commission publishes the List of Trusted Lists (LOTL) as signed,
machine-processable XML pointing at every national list, and both are
signed, so a certificate is trusted only if the list that names it
validates. The root of trust is the LOTL, not gazzettaufficiale.it. This
is the anchor of §4 supplied by the state, and it is standardised (ETSI
TS 119 612) with mature validators (the Commission's DSS).

This gives a check the certifier cannot influence: a record's
authenticity comes from the body that issued the law, not from whoever
built the collection. §6 says how it enters the pack.

## 5. `content: manifest-only` — nothing uncertified can exist

The operator's rule: in a certified collection only certified content
may be embedded or returned; anything else is discarded, reported in
search, and stops embedding. Made precise:

**Where the gate is.** "Cannot exist" is true only if enforced at
ingestion. Shards land in staging and are promoted into the corpus in
one sequential copy (§10); that copy is the gate: *a shard is promoted
only if its root is in the current, signed, anchored snapshot*. This
belongs with the refuse-to-boot invariants, not the deferrable errors.
The platform has already seen the failure this prevents (amendment 36:
a peer-copied shard indexed ack-free from a hand-populated folder).
Everything below is defence in depth for a gate that was bypassed.

**Granularity.** A chunk cannot be half-certified: chunking is a pure
function of one record (invariant 3) and a record is atomic. Three
units:

- **certification** — the shard (its root in the snapshot);
- **discard** — the record and all its chunks;
- **halt** — the space, on that node: embedding stops for the whole
  collection, no bundle is published, the node marks the collection
  *integrity violated* until re-fetch and re-verify.

Halting the space rather than the record is right: a bundle carrying
vectors for chunk ids outside the snapshot would poison everyone's
coverage map. Search continues over the certified subset, with the
exclusion visible in the results — "3 shards excluded: not in the
certified snapshot" — never a silent drop.

**Attribution.** The response is the same in every case (discard, halt,
flag); who is flagged is not, and "good faith" does not change it:

| found | who erred | who is flagged | caught by |
|---|---|---|---|
| bytes ≠ claimed root | the seeder served corrupt or forged data | the seeder | transport: torrent v2 checks pieces and bans the sender before anything reaches the corpus |
| root not in the signed snapshot | someone introduced foreign material | the local source of that shard | the promotion gate |
| signed, valid shard whose records ≠ origin (`records-vs-origin`, `origin-seal`) | the certifier: pipeline bug or key compromise | **the certifier and the snapshot** — the seeder served faithfully what was signed | verification against the origin |

The third row is the "honest mistake" question. The artefact cannot
distinguish a bug from a compromise and does not need to: a failing
deterministic check is a proof (§16), it names the shard, and the
certifier's standing falls either way — reliability of its pipeline is
the promise "certified" makes. The remedy is the same: a later snapshot
that passes. Humans tell bug from attack by reading the anchor's
history (rotation? revocation?), not the protocol.

## 6. How the seal appears in the pack

**6.1 The problem that matters is time.** A signature verifiable today
is unverifiable in twenty years: the certificate expires, the OCSP
responders go dark, the CRLs disappear, the trusted list of the day is
gone. An archive of law that cannot prove authenticity in thirty years
has failed at the one thing it was for. Keeping the signed PDF does not
help — the PDF is what becomes unverifiable.

What survives is the **validation evidence** captured while the
signature was still checkable: the certificate chain, the OCSP/CRL
responses of that moment, a timestamp, and the relevant trusted-list
snapshot. Kilobytes. This is what archival-grade signature profiles
(ETSI's LT/LTA levels) preserve, and for a certified collection it is
captured when the snapshot is produced, not later.

**6.2 The artefact.** Not the `.p7m`. Every record already carries
`origin_hash`, the hash of the origin bytes it was parsed from
(invariant 11). The chain is:

```
LOTL → IPZS certificate → seal → PDF bytes (origin_hash) → [pinned parser] → record text (content_hash)
```

The pack gains a small member, `seals/<snapshot>.json`, one entry per
sealed issue:

```
{origin_hash, signer, chain: [...], ocsp: [...], timestamp, tl_snapshot, verified_at}
```

The seal covers the *issue*; the records are the individual acts. Many
records point at one entry through `seal_ref`; nothing is duplicated.

**6.3 The check.** `origin-seal`, in the validation plan, in two modes:

- **live** — re-fetch the sealed file, validate the seal against the
  current LOTL, re-parse with the pinned parser, compare `content_hash`;
- **archival** — validate the seal against the stored evidence and the
  record's `origin_hash`; no network, works when the origin is gone.

The archival mode is what makes the collection *kept* in the sense the
tagline promises. It proves that *this* record came from *those* sealed
bytes as long as anyone holds the bytes — which the next section makes
optional but possible.

**6.4 Preserving the certified format.** Yes, as an **optional
member**, `origin-<slice>`, holding the `.p7m` files — not inside the
records. Whoever needs legal standing fetches it; whoever searches does
not. "Only useful data travels" (§0.5 rule 8) stays the default and the
legal use case is served. Without it the archival check still proves
provenance; with it, a peer can re-derive everything from the sealed
bytes alone.

## 7. The hidden risk: parsing PDF

Text extraction from PDF is the least deterministic parsing in the
platform — a library version changes and whitespace moves. On a
collection where bit-exactness is load-bearing this is the real fragile
point, more than any key. The Gazzetta also publishes structured forms
for part of its acts, and §6 of `pack-format.md` already requires
recording *which origin form* was chosen and why. The choice is binary
and must be made knowingly:

- parse the **sealed PDF** — the seal covers exactly what was parsed;
  the parser is fragile;
- parse the **structured form** — the parser is solid; the seal covers a
  *different* file, and the collection trusts IPZS to keep the two
  aligned.

For a certified collection: the sealed PDF, because authenticity is the
whole point. What makes the fragile side bearable is the frozen
toolchain of `no-central-node.md` §5.1: the Codex carries the digest of
the platform image, which pins the extraction library together with the
parser — `PARSER_VERSION` alone never did, since the same parser source
over a different library build produces different bytes. Drift is then
not an accident that goes unnoticed but a different image, which either
proves itself equivalent by reproducing the sample byte-for-byte or is
refused. The form choice is recorded beside the plugin as §6 requires.

## 8. Proposed amendments

62. **`pack-format.md` §7** — the Codex gains `publication`, `certifier`
    (key, anchor URL, `is_origin`), `embedding`, `succession`; and, for
    certified collections, the fixed `content: manifest-only`.
63. **`pack-format.md` §10** — shard promotion refuses any shard whose
    root is not in the current signed and anchored snapshot; for
    certified collections this is a refuse-class invariant with a test
    that promotes a foreign shard and expects rejection. A violation
    found after promotion halts the space on that node.
64. **`pack-format.md` §13** — the BEP 44 mutable "latest snapshot" item
    is retained for certified collections only, signed by the certifier;
    the anchor URL is the fallback.
65. **`pack-format.md` §15** — new checks: `anchor` (key fingerprint
    matches the well-known/DNS anchor), `origin-seal` (live and
    archival). `checks.required` in a certified Codex lists what must
    pass before the collection is *shown* as certified, not merely what
    may be run.
66. **`pack-format.md` §16** — an attestation signed by the certifier's
    key on `vectors-sample` is the *endorsed* embedding level; the Library
    shows certification and trust level as two axes ("certified by X ·
    reproduced by N verifiers"), never one.

## 9. Open questions

1. **Anchor fetch policy.** At adoption and then how often? A stale
   anchor misses a revocation; a chatty one hits the certifier's server
   from every peer. Daily with jitter is the first guess.
2. **Succession trigger.** `open-after` needs a definition of "silent":
   no snapshot for N cadences, or the anchor unreachable for N days, or
   both?
3. **Evidence freshness.** Validation evidence captured at snapshot time
   ages with the certificates; ETSI's answer is a periodic archival
   timestamp. Who applies it in a peerless system — every holder, on a
   schedule from the Codex?
4. **Intermediary certifiers.** When `is_origin: false` (this node
   building the Gazzetta from IPZS's files and signing), the Library
   must say so plainly; the wording matters and is not decided.
5. **Seal formats per origin.** CAdES for the Gazzetta, PAdES for the
   OJ; a plugin declares which, and the `origin-seal` check dispatches
   on it. Other national gazettes will add more.
