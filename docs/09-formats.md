# 9. The DRAG format — every byte that is hashed, signed or moved

A collection on disk is a **drag**: *distributed RAG*, the format this
document fixes. One directory holds one collection — its Codex, its
snapshots, its records, its vectors and everything else a peer shares
(02 §2.6) — and a peer that receives a drag can check every byte of it
against the identity it claims, which is what the name is for.

This document is the byte-level reference the other eight point at. It
fixes, for every artefact whose identity is a hash, whose authorship is
a signature, or whose bytes cross a wire, exactly what those bytes are:
the path in the drag or the message on the wire, the fields and their
types, the rule by which the bytes are hashed or signed, and one worked
example on the project's own collections. Documents 02–08 say what an
artefact is *for* and point here for what it *is*; nothing in 02–08
restates a field table, and nothing here changes a rule stated there.
Where a section names a module of the platform, that module is the
implementation of the section, and the two agree byte for byte.

Two hash functions, two roles. `H` is SHA-256, and it is the function
behind every identity and every signed object: `collection_id`,
`space_id`, `lineage_id`, the snapshot hash, shard roots and
`records_root`, bundle ids, attestation and event ids, the DHT keys and
the rendezvous scores. Its textual form is always `sha256:` followed by
64 lowercase hexadecimal digits. `content_hash` and `origin_hash` are
BLAKE2b with a 16-byte digest — 32 lowercase hexadecimal digits, no
prefix — and they are the only two places BLAKE2b appears: per-record
fingerprints inside shards, thirty-six million of them in PubMed, where
the cost of the function and the size of the field matter and where
neither is ever signed on its own. A reader that meets a digest can tell
the function from its length: 71 characters with the prefix is `H`, 32
without is BLAKE2b-128.

A signed object is signed as stored bytes, and a hashed object is hashed
as stored bytes. The producer writes the canonical form of §9.1 and that
file, or that message, is what the signature covers and what the hash
names; a verifier hashes the bytes it received and never a
re-serialisation of what it parsed. Parsing happens after the hash or
signature has been checked, for meaning only; a document that parses to
the same values under two different byte strings is two different
documents, and only the one whose bytes hash to the claimed identity is
the real one. A file on disk that is not in canonical form is not a
Louise artefact and is refused, never repaired.

## 9.1 Hashes and canonical JSON

**Canonical JSON.** Every hashed or signed JSON object is written in
one form, and only that form is ever hashed:

| rule | exactly |
|---|---|
| encoding | UTF-8, no byte-order mark, no trailing newline |
| object keys | strings, unique, sorted by Unicode code point of their UTF-8 bytes |
| whitespace | none outside strings; separators `,` and `:` |
| strings | JSON strings; non-ASCII characters unescaped; the escapes `\"`, `\\`, `\n`, `\r`, `\t`, `\b`, `\f` and `\u00XX` for the other control characters; every string is a sequence of Unicode scalar values — an unpaired surrogate is refused |
| integers | JSON numbers, no leading zeros, no sign on zero, no exponent |
| non-integer numbers | never a JSON number: a decimal string, `"0.95"`, digits with one optional `.`, no exponent, no leading `+` |
| `null`, `true`, `false` | allowed; `NaN` and `Infinity` refused |
| a field the schema lists whose value is absent | present as `null`, never omitted |
| a field the schema does not list | refused on any hashed or signed object |
| arrays | in the order the schema fixes; canonical JSON never sorts an array |

This is RFC 8785 restricted to no floating-point numbers. A parser that
reads a canonical object refuses a duplicate key, a float, an unpaired
surrogate and an unknown key, and refuses them before any value is used.
Blocks whose keys a plugin chooses — `origin.spec`, `profile`, a
record's `extra`, an attestation's `detail`, a seal entry's `signer` —
are opaque objects: their keys are free, they are canonicalised and
hashed like everything else, and a reader passes them through without
interpreting them.

**The one concatenation.** `H(a ‖ b ‖ …)` means SHA-256 over the
canonical JSON of the array `[a, b, …]` of strings, each string in its
textual form (`sha256:…` for an identity, `ed25519:…` for a key, a plain
string otherwise). There is no other concatenation: no separator bytes,
no length prefixes, no raw digests joined end to end, except inside the
bundle's signed payload (§9.7), which is framed and domain-separated on
its own terms.

```
H("sha256:3b1f…c2e0" ‖ "2026-08")  =  SHA-256( ["sha256:3b1f…c2e0","2026-08"] )
```

**BLAKE2b-128.** `content_hash` (§9.5) and `origin_hash` (§9.5, §9.17)
are BLAKE2b with `digest_size = 16`, no key, no salt, no
personalisation, over the bytes each section names; textual form 32
lowercase hexadecimal digits.

**Path segments from digests.** Where a digest names a directory — the
`<space>` segment under `vectors/` (02 §2.4), the `<set>` segment of the
calibration member (§9.12), a collection's directory (07 §7.8) — the
segment is the first 16 hexadecimal digits of the digest after the
prefix, except a collection's directory, which is all 64.

**The seeded sample.** Every sampled check draws its sample by one
function, `sample(seed, n, universe)`: sort the universe of ids by their
UTF-8 bytes; score each id by HMAC-SHA256 with key `seed` over the id's
UTF-8 bytes, reading the first 8 bytes of the tag as a big-endian
unsigned integer; take the `n` ids with the lowest scores, ties by the
id's bytes. A fraction `f` of a universe of size `N` is `n = max(1,
⌈f × N⌉)`. `seed` is 32 bytes: chosen by the verifier from its own
randomness and published in the attestation (§9.8), so that a sample is
reproducible by anyone once published and predictable by no one before;
or, where a rule names a deterministic seed — the k-means sample of
§9.6 is seeded with the snapshot hash's raw 32 bytes — the same function
with that seed. A plan (§9.3, `validation`) fixes `n` or `f` and never
the seed.

**Times.** Every time in a Louise object other than a bundle header is a
string, RFC 3339 in UTC to the second: `2026-09-01T04:00:00Z`. The
bundle header's `created_at` is an integer of Unix seconds (§9.7).
Durations are ISO 8601, `P1M`, `P6M`, `P14D`; `seconds(d)` counts a month
as thirty days and a year as 365.

**Worked example.** The PubMed collection's identity is `H` of the
canonical JSON of its Codex with the `descriptive` block removed
(§9.3), written `sha256:9d3e…41ab` — `H` of the file's own bytes is its
`codex_hash`, a different value; its September cutoff snapshot is announced under
the DHT key made of the first 20 bytes of
`SHA-256(["sha256:9d3e…41ab","2026-08"])` (§9.16); a record in it,
PMID 38112034, carries `content_hash` `a3f0…7c19` (BLAKE2b-128 of its
cleaned title and abstract, §9.5) and `origin_hash` `5e21…0b44`
(BLAKE2b-128 of the update file it was parsed from).

## 9.2 Keys and signatures

**Keys.** A peer is one Ed25519 key pair. The textual form of a public
key is `ed25519:` followed by 64 lowercase hexadecimal digits (the 32
raw bytes). The private key lives at the path `NODE_KEY_PATH` names
(07 §7.8) as either the 32 raw seed bytes or the same seed as 64
hexadecimal digits with an optional trailing newline; nothing else — no
PEM, no passphrase, no algorithm field. On a POSIX filesystem the file
is refused when group- or world-accessible. The key material never
appears in a log, an error message, a config value or any object below;
the public key appears everywhere a producer or verifier is named.
Ordinary keys do not rotate: a lost key is a new key, and the bundles
and attestations the old key signed stay valid under the old key. A
certifier's key rotates at its anchor (§9.11), and only there.

**Signatures.** Ed25519 (pure, RFC 8032), 64 bytes. In a JSON object a
signature is 128 lowercase hexadecimal digits; in the bundle it is the
64 raw bytes of `MANIFEST.sig` (§9.7).

**What is signed.** Every signed object other than the bundle is signed
over

```
message = <domain> ‖ 0x00 ‖ SHA-256(canonical JSON of the body)
```

where `<domain>` is the ASCII bytes of one of six strings and the hash
is the 32 raw bytes of the body's canonical-JSON digest:

| domain | signs |
|---|---|
| `louise/snapshot/v1` | a snapshot, by its hash (§9.4) |
| `louise/attestation/v1` | an attestation body (§9.8) |
| `louise/digest/v1` | a catalogue digest (§9.9) |
| `louise/event/v1` | an event body, and a pull request (§9.9) |
| `louise/pointer/v1` | a certifier's pointer (§9.11) |
| `louise/calibration/v1` | the calibration member's statement (§9.12) |

The bundle is signed over its own framed payload under the domain
`louise-rag/bundle-sig/v1` (§9.7). A signature made under one domain
verifies under no other, so a key that signs bundles, snapshots and
digests can never be made to sign one thing while meaning another.

**The envelope.** Except for the snapshot (whose signatures live in a
sidecar, §9.4) and the bundle, a signed object travels and is stored as
one JSON object:

```
{"body": {…}, "sig": "<128 hex>"}
```

`body` is the canonical object the section defines; it always carries
`kind`, `version`, `key` (the signer, `ed25519:…`) and `time`. `sig` is
the signature of `body.key` over the message above. The envelope's own
bytes are canonical JSON too. The object's id, where it has one, is
`H(canonical JSON of body)` — never of the envelope — so two peers that
receive the same body compute the same id whatever byte string carried
it, and an id is never a field of the body it names.

**The trust file.** A peer's trust list is `state/trust.json` (07 §7.8),
local, never signed, never sent:

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-trust"` | yes | |
| `version` | `1` | yes | |
| `allow` | list of keys | yes | keys whose attestations are counted when the list is used as an allow filter; empty means every key is counted. It is also what `hold vectors import --strict` consults locally (07 §7.3), the one place the allow side touches a bundle; a bundle's admission is otherwise 03 §3.5's ladder |
| `deny` | list of keys | yes | keys whose attestations, digests and bundles are ignored |
| `quarantined` | list of `{key, attestation, time}` | yes | keys burned on this peer by a proof of forgery (03 §3.5): `attestation` is the id of the `forgery` attestation; the entry is removed only by `hold key trust <key> --restore` |

A list is a filter over whose word is counted at all, never a weight
(08 §8.1, 03 §3.6).

**Worked example.** A peer verifying the Gazzetta Ufficiale's snapshot
`2026-08` reads the sidecar entry `{key: "ed25519:4a…", time:
"2026-09-01T06:12:40Z", sig: "…"}`, takes the snapshot file's hash
`sha256:c07d…` (§9.4), builds `"louise/snapshot/v1" ‖ 0x00 ‖ 0xc07d…`
(the 32 raw bytes), and verifies the 64-byte signature against
`ed25519:4a…`; then checks that `ed25519:4a…` is the key the Codex's
`certifier` block names and that the anchor (§9.11) lists it as valid at
`time`.

## 9.3 The Codex file

**Path.** `codex.json` at the root of the drag (02 §2.6). Its bytes are
the canonical JSON of the object below and nothing else — no pretty
form exists.

**Two hashes.** `codex_hash = H(bytes of codex.json)` is the file: what
a peer fetched, what a signature over the file covers, and what a
mismatch on receipt refuses. `collection_id` is the identity, and it is
`H` of the canonical JSON of the same object with exactly one key
removed — `descriptive` — so that a title and a summary travel inside
the Codex, signed and tamper-evident, without deciding what the
collection is (02 §2.1):

```
inner = the codex object, the key "descriptive" removed entirely
        (removed, not set to null: a null value is a different object)
collection_id = H(canonical-json(inner))
codex_hash    = H(bytes of codex.json)
```

Removing one named key is the whole rule; no field list is maintained
anywhere, and a later version that adds a descriptive field adds it
inside `descriptive` and moves no identity. The collection's directory
under `<LOUISE_ROOT>/collections/` is named by the 64 hexadecimal
digits of `collection_id` (07 §7.8). A file whose inner hash does not
equal the id its directory claims is refused; so is one pulled from a
peer whose bytes do not equal the `codex_hash` that peer's digest
advertised (§9.9). No snapshot carries a `codex_hash`: a snapshot is
byte-identical across peers and `codex_hash` is not, since `descriptive`
is outside the identity and inside the file. On the signed paths — the
`codex` event, which carries Codex objects (§9.9) — the signature covers
them and no separate hash is needed.

**Fields.** Every field is present; a field not in use is `null`. Nested
blocks are objects; the types are canonical-JSON types (§9.1), and a
number written as a decimal string is marked *decimal*.

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-codex"` | yes | |
| `version` | `1` | yes | the Codex format version (§9.18) |
| `descriptive` | object | yes | `{title: string, summary: string}` — for people. The one key outside `collection_id`; covered by `codex_hash`; displayed, never used to sort, merge or resolve (08 §8.2). `title` is one line; `summary` is one short paragraph. Neither may be empty |
| `plugin` | string | yes | the plugin's `source_id` (06 §6.1); the uid prefix |
| `origin` | object | yes | the origin block, below |
| `toolchain` | object | yes | `{platform_image, mother_platform_image}` — image digests, `sha256:…`; `mother_platform_image` is `null` except on a daughter, where it is the mother's `platform_image` |
| `parser` | object | yes | `{id: string, version: integer}` — the readable alias of what the platform image contains |
| `cleaning_version` | integer | yes | the cleaner's version in the platform image (06 §6.3) |
| `protocol_version` | integer | yes | the plugin protocol version the plugin implements (06 §6.1) |
| `layers` | list of layer | yes | `{chunker: {id, version, params}, rule}`, in evaluation order; the last rule is `"default"` (06 §6.3). `params` is an object whose keys the chunker id fixes, `{}` when it takes none |
| `slicing` | object | yes | below |
| `space` | object | yes | below |
| `work` | object | yes | `{tokens: integer, per: duration}` — the work parameter (02 §2.4): tokens of the encoder's tokenizer per `per`; `per` defaults to the update cadence when the recipe omits it, and is always written out |
| `modality` | `"text"` · `"dna"` | yes | what the bytes are; decides encoder and reranker eligibility (06 §6.1) |
| `erasure` | `"append-only"` · `"retractable"` · `"erasable"` | yes | how content may leave (04 §4.4) |
| `validation` | object | yes | the plan (03 §3.2), below |
| `license` | object | yes | `{class: "green" · "yellow" · "red", terms_sha256: "sha256:…" or null}`; `null` on green and only on green |
| `privacy` | `"public"` · `"restricted"` · `"never-share"` | yes | 04 §4.4; `erasure: "erasable"` requires `"never-share"` |
| `update` | object | yes | `{cadence: "monthly" · "weekly", day: integer, grace_days: integer ≥ 0}`. `day` is read by the cadence: 1–28 as a day of the month when monthly, 1–7 as an ISO weekday when weekly. Any other value is refused. The cadence fixes the snapshot-id form (§9.4) and so is fixed for a chain: a daughter continues her mother's chain and keeps it, and only a sister, who begins her own, may change it (02 §2.7) |
| `publication` | `"open"` · `"certified"` | yes | 02 §2.8 |
| `certifier` | object or null | yes | `{key: "ed25519:…", anchor: "https://…", is_origin: boolean}`; non-null exactly when certified |
| `embedding` | `"open"` · `"endorsed"` · `"certifier-only"` | yes | 03 §3.8; `"open"` on an open collection |
| `succession` | `"frozen"` or `{"open-after": duration}` | yes | 02 §2.8; `"frozen"` on an open collection |
| `content` | `"manifest-only"` or null | yes | `"manifest-only"` exactly when certified |
| `continues` | id or null | yes | daughter: the mother's `collection_id` |
| `continues_through` | snapshot id or null | yes | daughter: the last snapshot that is the mother's — the boundary, named from the side that already exists (02 §2.7). Everything after it is the daughter's |
| `origin_reachable_until` | snapshot id or null | yes | daughter: the last snapshot whose shards the origin still reproduces |
| `derived_from` | id or null | yes | sister and corrective sister: the mother's `collection_id` |
| `space_from` | id or null | yes | sister: the Codex whose `space` and `layers` were copied byte for byte. `cleaning_version` is not among them: a sister keeps her mother's in any case, since changing it would re-derive the corpus she inherits (02 §2.7) |
| `replaces` | object or null | yes | corrective sister: `{codex: id, defect: {check, toolchain, witnesses, expected, observed, rationale}}` (03 §3.6); `witnesses` is a non-empty list of shard names `"<slice>/<n>"` or uids |
| `facets` | list of objects | yes | which record fields a search may filter on and a reader may list (05 §5.7), sorted by `field`, `[]` when none. Each is `{field: string, kind: "flat" · "tree" · "range", separator: string or null, authority: {name: string} or null}`; `separator` is non-null exactly when `kind` is `"tree"`. `field` is a top-level record field (§9.5) or `extra.<key>`. Declares structure only: the values live in the records |
| `credits` | list of objects | yes | the pre-embedded archives whose vectors this collection carries, sorted by `archive`, `[]` when none. Each is `{archive: string, producer: string, key: key, license: string, attribution: string, coverage: "all" or {ids_per_shard: integer, shards: integer}}`, where `key` is the `ed25519:…` its bundles are signed with, so an uncredited archive's bundle can be refused (03 §3.5). Derived at creation from the platform's registry and from what is inherited, never composed by hand (02 §2.7); hashed, so neither the licence, the attribution nor the coverage can be altered by a later build |
| `profile` | object or null | yes | plugin-defined descriptor, opaque and hashed (an ELI profile: `{eli: {jurisdiction, work_pattern, …}}`) |

The relations are exclusive in this way: a daughter has `continues` and
`continues_through`; a sister has `derived_from` and, when it was made with
`--space-from`, `space_from`; a corrective sister has `derived_from` and
`replaces`; a root has none of them. Any other combination is refused.
A daughter's `credits` equal her mother's, since she inherits the
vectors; a sister's equal her mother's when her `space_id` matches her
mother's and are her own recipe's when it does not, there being no
third case; a corrective sister's are `[]`, since it starts from zero
(02 §2.7).

**The origin block.**

| field | type | required | meaning |
|---|---|---|---|
| `spec` | object | yes | the identifying half of the acquisition spec, serialised as §9.17 fixes |
| `form` | string | yes | which origin form was taken and why, one sentence (06 §6.4) |
| `unit` | string | yes | how a record's origin bytes are reached, which is the cost of verifying one: `"file"` the origin publishes the document alone, `"container-member"` a container must be fetched to reach it, `"index-page"` an index makes the container seekable (06 §6.4). It never changes what `origin_hash` covers — that is always the one document (02 §2.3) |
| `clock` | string | yes | IANA zone of the origin's clock: `"America/New_York"`, `"UTC"`, `"Europe/Rome"` |
| `cutoff_rule` | `"dated-dumps"` · `"update-files"` · `"record-date"` | yes | how the origin's state at the cutoff instant is enumerated (06 §6.2) |
| `jurisdiction` | string or null | yes | ISO 3166-1 alpha-2, for a legal corpus; `null` otherwise |

**The slicing block.** `keys` is the ordered list of slice keys; each
key has a block; `shard_records` and `order` close the shard rule.

| field | type | required | meaning |
|---|---|---|---|
| `keys` | list of strings | yes | slice keys in path order: `["year"]`, `["lang", "pages"]`, `[]` for one slice `all` |
| `<key>` | object | one per key | for a year key: `{field, bins}` — `field` names the record field read (`date`), `bins` is the list below; for an id-range key: `{field, bin: integer}` — the record's `native_id` divided by `bin`; for a language key: `{field: "lang"}` |
| `unknown` | string | yes | the slice a record joins when the key cannot be read: `"year=unknown"` |
| `shard_records` | integer | yes | records per shard: 30,000 for PubMed, 2,000 for Wikipedia, 50 for Gutenberg |
| `order` | `"bytes"` · `"numeric"` | yes | how records sort within a slice: UTF-8 byte order of `native_id`, or integer order of it |

The `bins` grammar, evaluated in list order over a year `Y`: `"<N"` —
one bin for every year before `N`, named `pre<N>`; `"<k>y<N"` — bins of
`k` years from the previous bound up to `N`, named `<lo>-<hi>`; `"1y"`
— one bin per year from the previous bound on, named `<Y>`. A slice
name is `<key>=<value>` for each key in order, joined by `/`:
`year=1975-1979`, `year=2023`, `lang=de/pages=3M` (`pages` = `native_id
// bin`, written with `M` for a bin of one million). A slice name
matches `^[A-Za-z0-9=_\-][A-Za-z0-9=_.\-]*(/[A-Za-z0-9=_\-][A-Za-z0-9=_.\-]*)*$`;
it becomes a directory under `records/`.

**The space block.**

| field | type | required | meaning |
|---|---|---|---|
| `encoder` | object | yes | `{image: "sha256:…", weights: "sha256:…", label: string}` — the encoder container's digest, the digest of the weights file it serves, and a readable label without guarantee. The encoder image is named here and nowhere else: `toolchain` pins the platform build only (02 §2.1), and §9.18 would refuse a Codex carrying a third `toolchain` key |
| `dim` | integer | yes | vector width as stored |
| `dtype` | `"f16"` · `"f32"` | yes | storage type of vectors |
| `ctx` | integer | yes | the encoder's input window in tokens; a chunk is truncated to it |
| `pooling` | string | yes | `"cls"`, `"mean"`, `"last"` — the pooling the encoder is run with |
| `normalise` | boolean | yes | whether vectors are unit-normalised before storage; `true` in every text space |
| `query` | object | yes | `{encoder: {image, weights} or null, instruction: string or null}` — an asymmetric space's query encoder, with its own image and weights digests, and its prefix; both `null` in a symmetric space |
| `tolerance` | decimal | yes | the cosine a vector must reach against the reference (03 §3.1) |
| `calibration` | object | yes | `{set: "sha256:…", vectors: "sha256:…", quorum: {n: 16, min_pass: 12}}` — the calibration set's id, the reference vectors' hash (§9.12), and the quorum |
| `envelope` | id | yes | the hash of the envelope tensor in the calibration member (§9.12) |
| `epoch` | string | yes | `"0"` for a root; a daughter AND a sister keep their mother's, so a sister of a corrective sister keeps the salted one and stays in her mother's space (02 §2.7); a corrective sister's is `H(canonical JSON of {"replaces": <replaced collection_id>})`, written `sha256:…`. Any other value is refused |
| `evaluation` | object or null | yes | `{suite: {name, version, sha256, public}, metric, value: decimal, conditions: {…}, coverage_check: {targets, resolved}, run_by: key, run_at: time, tier: "reproducible" · "asserted"}`; `null` when no score is claimed |

**space_id.**

```
space_id = H( canonical JSON of
  {"cleaning_version": <int>,
   "encoder":          {"image": "<space.encoder.image>", "weights": "<space.encoder.weights>"},
   "layers":           <the layers list, byte for byte>,
   "space":            {"ctx", "dim", "dtype", "epoch", "normalise", "pooling", "query"}} )
```

Not `tolerance`, not `calibration`, not `envelope`, not `evaluation`,
not `work`: those are the contract around the function, not the
function. `space_from` copies `space` and `layers` byte for byte and
never `cleaning_version`, which stays the mother's (07 §7.3); `derive`
refuses when the named Codex's `cleaning_version` differs from hers, and
refuses in any case when the resulting `space_id` differs from the
source's. The `<space>` path segment is the first 16
hexadecimal digits of the digest.

**The validation block.**

| field | type | required | meaning |
|---|---|---|---|
| `checks` | list of check names | yes | the checks that apply, from 03 §3.2's table of plan checks, in the order they run. §9.8's table is wider: it adds the results that are never planned — `merge`, `forgery`, `withdraw`, `supersedes` — which a peer produces from what it finds, not from a plan |
| `sample` | object | yes | per sampled check, its size: `{"records": {"shards": decimal}, "chunks": {"shards": decimal}, "records-vs-origin": {"shards": decimal}, "vectors-sample": {"shards": decimal}}` — a decimal is a fraction of the snapshot's shards, `"1"` is exhaustive. The plan sets how many SHARDS a check samples and never how many chunks: `vectors-sample` draws sixteen per sampled shard and passes on the quorum `{n: 16, min_pass: 12}` of 03 §3.3, the same sixteen T3 draws by the importer's private randomness (03 §3.5). A plan-set count would need a scaling rule for the threshold that a creator could get backwards — raising the sample to strengthen the check and weakening it instead — and would drift from T3, whose randomness is deliberately unpublished |
| `required` | list of check names | yes | certified collections: what must have passed before the collection is shown as certified (03 §3.7); `[]` on an open collection |

`validation.json` at the drag root is this block written out in
canonical JSON for tools; `hold check` reads it and refuses to run when
its bytes differ from the block in the Codex (03 §3.2).

**lineage_id** is not a field. It is the `collection_id` of the root
reached by following `continues` only; a peer computes it from the chain
of Codexes it holds, and the chain travels with every Codex (§9.9,
`pull codex`).

**Recipe.** `hold collection create <plugin> --recipe FILE` reads a
TOML file whose tables and keys are the Codex fields the plugin does not
default (06 §6.6); the recipe is input, never hashed, never shared.

**What `hold collection show --json` prints.** One object: `{codex:
<the Codex>, collection_id, space_id, lineage_id, relation: "root" ·
"daughter" · "sister" · "corrective-sister", snapshots: [{snapshot_id,
hash, signatures: integer, held: boolean}], holdings: {slices: [...],
kinds: [...], members: integer}, license: {class, acknowledged: boolean,
time}, trust_level, certification: {certified_by, anchor_state} or null,
vitality: "actively-held" · "dormant", refused: string or null}`; the
last is the reason the collection is not served when refuse-to-start
applies to it (07 §7.8).

**Worked example.** The PubMed collection's Codex, abbreviated to the
fields that carry its choices (every other field is present in the file):

```
{"kind":"louise-codex","version":1,
 "descriptive":{"title":"PubMed","summary":"Every MEDLINE abstract, sliced by year, for biomedical retrieval."},
 "plugin":"pubmed",
 "origin":{"spec":{…ftp.ncbi.nlm.nih.gov/pubmed/baseline and updatefiles…},
           "form":"the XML baseline and update files, because the API paginates and the FTP files are checksummed",
           "unit":"container-member","clock":"America/New_York","cutoff_rule":"update-files","jurisdiction":null},
 "toolchain":{"platform_image":"sha256:1f…","mother_platform_image":null},
 "parser":{"id":"pubmed","version":4},"cleaning_version":1,"protocol_version":1,
 "layers":[{"chunker":{"id":"abstract","version":1,"params":{}},"rule":"default"}],
 "slicing":{"keys":["year"],"year":{"field":"date","bins":["<1950","5y<2000","1y"]},
            "unknown":"year=unknown","shard_records":30000,"order":"numeric"},
 "space":{"encoder":{"image":"sha256:8c…","weights":"sha256:e2…","label":"MedCPT-Article-Encoder"},
          "dim":768,"dtype":"f16","ctx":512,"pooling":"cls","normalise":true,
          "query":{"encoder":{"image":"sha256:0a…","weights":"sha256:b7…"},"instruction":null},
          "tolerance":"0.95","calibration":{"set":"sha256:6b…","vectors":"sha256:d4…","quorum":{"n":16,"min_pass":12}},
          "envelope":"sha256:77…","epoch":"0","evaluation":null},
 "work":{"tokens":40000000,"per":"P1M"},"modality":"text","erasure":"retractable",
 "validation":{"checks":["roots","records","chunks","records-vs-origin","vectors-sample","calibration","centres","labels","license","signature"],
               "sample":{"records":{"shards":"0.05"},"chunks":{"shards":"0.05"},"records-vs-origin":{"shards":"0.05"},"vectors-sample":{"shards":"0.10"}},
               "required":[]},
 "license":{"class":"red","terms_sha256":"sha256:b9…"},"privacy":"public",
 "update":{"cadence":"monthly","day":1,"grace_days":7},
 "publication":"open","certifier":null,"embedding":"open","succession":"frozen","content":null,
 "continues":null,"continues_through":null,"origin_reachable_until":null,
 "derived_from":null,"space_from":null,"replaces":null,"profile":null}
```

`collection_id` is `H` of these bytes with the keys sorted, the
whitespace removed and the `descriptive` block dropped; `codex_hash` is
`H` of the file exactly as written, `descriptive` included; the space segment under `vectors/` is the first 16
digits of `H` over `{"cleaning_version":1,"encoder":{"image":"sha256:8c…",
"weights":"sha256:e2…"},"layers":[…],"space":{"ctx":512,
"dim":768,"dtype":"f16","epoch":"0","normalise":true,"pooling":"cls",
"query":{…}}}`.

## 9.4 The snapshot file and its sidecar

**Paths.** `snapshots/<snapshot_id>.json` — the snapshot, canonical
JSON, no signature inside; its hash is `H(bytes of the file)`.
`snapshots/<snapshot_id>.sig.json` — the sidecar of signatures, a set
that grows and is never hashed. Superseded record shards keep their old
bytes beside the new ones (§9.5), so every snapshot in the chain stays
servable and checkable.

**The id.** A snapshot is named for the period of the collection's
cadence that CLOSED at its cutoff instant, and the cutoff is 00:00:00 on
`update.day` of the period that follows, in `origin.clock`.

| cadence | cutoff | id | example |
|---|---|---|---|
| `monthly` | 00:00:00 on day `update.day` (1–28) of month `M+1` | `YYYY-MM` of `M` | `2026-09-01T00:00:00 America/New_York` names `"2026-08"` |
| `weekly` | 00:00:00 on ISO weekday `update.day` (1–7) of ISO week `W+1` | `YYYY-Www` of `W` | `2026-09-07T00:00:00 America/New_York` names `"2026-W36"` |

Both forms are fixed-width, zero-padded and sort lexicographically
within a chain, which is all any DHT key (04 §4.2), path or member name
needs of them. A chain never mixes them: the cadence is in the Codex and
a daughter keeps her mother's. A record belongs
to the snapshot when the origin artefact it was parsed from is one the
Codex's `cutoff_rule` enumerates at that instant (06 §6.2).

**Fields.**

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-snapshot"` | yes | |
| `version` | `1` | yes | |
| `collection_id` | id | yes | the Codex this snapshot applies |
| `snapshot_id` | `YYYY-MM` or `YYYY-Www` | yes | by the cadence, below |
| `previous` | id or null | yes | the hash of the snapshot before; `null` on a root's first; on a daughter's first, the hash of the snapshot named by the Codex's `continues_through` |
| `shards` | list of shard | yes | every record shard the snapshot consists of, sorted by (`slice` bytes, `n`) |
| `relations` | list of relation shard | yes | the relations shards (§9.14), same order; `[]` when the collection has none |
| `files` | object | yes | every whole-collection member with its transport root: `{"validation.json": {root, bytes}, "LICENSE-TERMS": {root, bytes} or null, "calibration/<set>.safetensors": {root, bytes}, "calibration/<set>.sig.json": {root, bytes}, "seals/<snapshot_id>.json": {root, bytes} or null}`. A drag file that is neither here nor a shard a `shards` entry names is not a member and is not served (07 §7.8) |
| `delta` | object | yes | `{added: [uid…], changed: [uid…], tombstoned: [{uid, reason}…]}`, each list sorted by uid bytes; `reason` is `"retracted"` (the origin says the record should not have existed) or `"withdrawn"` (the origin no longer supplies it) |

A shard entry:

| field | type | required | meaning |
|---|---|---|---|
| `slice` | string | yes | the slice name (§9.3) |
| `n` | integer | yes | the shard number within the slice, from 1; the file is `records/<slice>/<n as five digits>.jsonl.zst` |
| `root` | id | yes | the BitTorrent v2 pieces root of the stored `.jsonl.zst` file (§9.5) |
| `records_root` | id | yes | SHA-256 over the shard's `content_hash` values as raw 16-byte digests concatenated in record order |
| `first_id`, `last_id` | string | yes | the `native_id` of the first and last record |
| `records` | integer | yes | records in the shard |
| `chunks` | integer | yes | chunks over all its records under the Codex's layers |
| `chunks_root` | id | yes | SHA-256 over, in chunk order, each chunk's id as UTF-8 bytes followed by the raw 16-byte BLAKE2b-128 of its text. It depends on the records and the Codex's layers alone — exactly as the `chunks` count above already does — so "the snapshot is records only" (02 §2.2) still holds, and computing it costs the chunking pass the count costs anyway |
| `tokens` | integer | yes | `chunks × space.ctx` — the upper bound §4.5 and §4.7 use |
| `bytes` | integer | yes | size of the stored file |
| `toolchain` | `"own"` · `"mother"` | yes | which `platform_image` produced it: the Codex's own, or on a daughter the mother's for inherited shards |

A relations shard entry has `slice`, `n`, `root`, `edges` (integer) and
`bytes`; it sits beside the record shard of the same slice and number.

**Shards, exactly.** Within a slice, records sort by `native_id` under
`slicing.order`; they are cut every `shard_records` records; shards are
numbered from `00001` in that order. A later snapshot re-issues a
changed record's shard under the same number with a new `root`; new
records fill the slice's open shard — its highest-numbered, and the
only one that has not yet held `shard_records` records — re-issuing it
under its number, and open a new number only once it is full. Having
held that many closes it for good: dropping a tombstoned record at a
later re-issue leaves it short and it is never refilled, so "open" is
a shard that has never been full, not one that is not full now; a
tombstoned record stays in
its closed shard until that shard is next re-issued and is excluded from
search and embedding by the tombstone alone. The set of shards re-issued
by a snapshot is the set whose `root` differs from the same `(slice, n)`
in `previous`; nothing lists it separately.

**Chain rules.** Consecutive snapshots have consecutive ids — the next
period of the collection's cadence, `2026-08` then `2026-09`,
`2026-W36` then `2026-W37`; each names
the hash of the one before; all name one `collection_id`, except a
daughter's first, whose `previous` belongs to the Codex named by
`continues` and whose inherited shards carry the mother's roots and
`toolchain: "mother"`. A peer applies a snapshot only when it holds the
one `previous` names.

**Re-running `hold snapshot create`.** For an id that exists: identical
bytes → nothing to do, exit 0; different bytes → refused, exit 2, the
first differing shard named; while the fetch for that cutoff is
incomplete → deferred, exit 3 (07 §7.7).

**The sidecar.**

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-snapshot-sig"` | yes | |
| `version` | `1` | yes | |
| `snapshot` | id | yes | the hash of the snapshot file these signatures cover |
| `signatures` | list of `{key, time, sig}` | yes | sorted by (`key`, `time`); `sig` is over `"louise/snapshot/v1" ‖ 0x00 ‖ <the 32 raw bytes of snapshot>` |

A signature says *I computed this too* (02 §2.2); a signature by the
Codex's `certifier.key` is the certification. Two peers merge sidecars
by union; a signature whose `snapshot` does not equal the file's hash is
dropped. A snapshot arriving without any verified signature is held in
staging and not promoted (04 §4.3).

**Worked example.** PubMed, the snapshot closing at the September cutoff:

```
{"kind":"louise-snapshot","version":1,"collection_id":"sha256:9d3e…41ab","snapshot_id":"2026-08",
 "previous":"sha256:51c0…2e7a",
 "shards":[{"slice":"year=1975-1979","n":1,"root":"sha256:0be4…","records_root":"sha256:a91c…",
            "first_id":"1","last_id":"137402","records":30000,"chunks":30000,"tokens":15360000,
            "bytes":10485760,"toolchain":"self"}, …,
           {"slice":"year=2026","n":3,"root":"sha256:7d20…","records_root":"sha256:44f1…",
            "first_id":"40931122","last_id":"40952017","records":18317,"chunks":18317,"tokens":9378304,
            "bytes":6291456,"toolchain":"self"}],
 "relations":[],
 "files":{"LICENSE-TERMS":{"root":"sha256:b9…","bytes":4210},
          "calibration/6b1a3f0e9c2d4b7f.safetensors":{"root":"sha256:d4…","bytes":1573376},
          "seals/2026-08.json":null},
 "delta":{"added":["pubmed:40931122",…],"changed":["pubmed:38112034",…],
          "tombstoned":[{"uid":"pubmed:38004411","reason":"retracted"}]}}
```

Its sidecar after two peers computed it and a third signed it later:

```
{"kind":"louise-snapshot-sig","version":1,"snapshot":"sha256:c07d…",
 "signatures":[{"key":"ed25519:19…","time":"2026-09-01T05:40:02Z","sig":"…"},
               {"key":"ed25519:4a…","time":"2026-09-01T06:12:40Z","sig":"…"},
               {"key":"ed25519:c3…","time":"2026-09-03T11:00:19Z","sig":"…"}]}
```

## 9.5 Record shards

**Paths.** `records/<slice>/<NNNNN>.jsonl.zst` is the newest version of
shard `n` of the slice. When a snapshot re-issues the shard, the file it
supersedes is renamed `records/<slice>/<NNNNN>.<last snapshot id>.jsonl.zst`
— the last snapshot in which those bytes were current — and kept, so
that snapshot can still be served and checked (01, promise 7).

**The record line.** One record is one line: a JSON object with exactly
these keys, written in this order, compact (no whitespace outside
strings), UTF-8 with non-ASCII unescaped, terminated by `\n`. The
order is fixed so that two writers produce the same bytes; a line is
canonical JSON with the key order fixed by this table instead of by code
point.

| key | type | required | meaning |
|---|---|---|---|
| `uid` | string | yes | `<plugin>:<native_id>` (06 §6.1) |
| `native_id` | string | yes | the origin's own id; a string always, even when numeric |
| `lang` | string | yes | ISO 639-1 (639-3 where no two-letter code exists); `"und"` when unknown |
| `title` | string | yes | cleaned; `""` when the origin has none |
| `creators` | list of strings | yes | in the origin's order; `[]` when none |
| `date` | string or null | yes | `YYYY`, `YYYY-MM` or `YYYY-MM-DD` |
| `subjects` | list of strings | yes | in the origin's order |
| `keys` | list of strings | yes | canonical ids `<namespace>:<id>` the document is about (06 §6.5); sorted by bytes |
| `sections` | list of `[heading, text]` | yes | the cleaned text in order (06 §6.1): UTF-8, NFC, LF newlines, no trailing whitespace on any line; `[]` on a pointer record |
| `extra` | object | yes | plugin-defined, fixed keys; `origin_url` on a red-class source; `in_force` on a legal source; `seal` on a certified source whose origin file carries a seal, holding that file's `file_hash` (§9.15); `reason` never (a reason belongs to the tombstone) |
| `origin` | string | yes | the locator of this record's own origin bytes: their path relative to the origin base or their URL, and where the document is a member of a container the path followed by `#` and the plugin's member index (06 §6.4); never an absolute local path |
| `content_hash` | BLAKE2b-128 | yes | below |
| `origin_hash` | BLAKE2b-128 | yes | BLAKE2b-128 of this document's own origin bytes, the ones `origin` locates — one record, one hash, whatever the origin ships them in (02 §2.3); a record without one is refused at build |

**content_hash** = BLAKE2b-128 of the canonical JSON of the array
`[title, [[heading, text], …]]` — the title and the sections exactly as
the chunker sees them, so the same hash means the same chunks and the
same vectors whatever the origin's own revision fields say. A pointer
record (below) carries the `content_hash` of the text it points at.

**Chunks** are not stored: a record's chunks are recomputed from its
`sections` by its layer's chunker (06 §6.3), and a chunk id is
`<uid>:<seq>:<chunker version>`; the `chunks` check reproduces them and
the snapshot carries, per shard, the count and the `chunks_root` (§9.4)
it reproduces against. Without a published root the check could compare
only a count, and a chunker that moved every boundary without changing
the number would pass — which 03 §3.4 leans on as half its proof that a
new platform image is equivalent.

**Pointer records.** A red-class collection's records member is the same
line with `sections: []` and `extra.origin_url` set; every other field,
`content_hash` and `origin_hash` included, is that of the full record.
The text lives in the state directory of each peer that fetched it from
the origin (04 §4.4) and is never a member. The `records` check
recomputes `content_hash` only where the text is; on a pointer shard it
checks the presence and form of the two hashes and nothing else.

**The file.** The lines are concatenated in record order and compressed
as independent zstd frames of 1,000 records each (the last frame
shorter), zstd 1.5.x at level 9 as pinned by the platform image, no
dictionary, no checksum flag, content size written in each frame header.
After the last data frame comes one zstd skippable frame carrying the
seek index; standard zstd tooling ignores it, so `zstd -d
shard.jsonl.zst` prints the lines. All integers little-endian:

```
skippable frame = magic u32 (0x184D2A5E) ‖ size u32 ‖ payload
payload         = "LOUISE-PACK-IDX1" (16 bytes) ‖ raw_len u32 ‖ zstd(index JSON) ‖ frame_len u32 ‖ "LPK1"
```

The magic is `LOUISE-PACK-IDX1` and not the format's name: it was
written into every shard built before the format was named, changing it
would make those shards unreadable for nothing, and no reader chooses
its behaviour from it.

`frame_len` is the length of the whole skippable frame (its 8-byte
header included), so a reader takes the last 8 bytes, seeks back
`frame_len`, and decodes. The index JSON, compact, keys in this order:

```
{"records": N, "chunks": C,
 "frames": [[offset, csize, rsize, n, first_uid], …],     -- absolute file offset, compressed and raw sizes, lines, first uid
 "uids": {uid: [frame, line], …}}                          -- in record order
```

**Roots.** `root` is the BitTorrent v2 pieces root of the stored file
(BEP 52): leaves are SHA-256 of consecutive 16 KiB blocks of the file,
the last block as long as it is; the leaf count is padded to the next
power of two with leaves of 32 zero bytes; each parent node is SHA-256 of its
two children concatenated; a file of at most one block has its single
leaf as root. `records_root` is SHA-256 over the raw 16-byte
`content_hash` digests concatenated in record order. Transport, `roots`
and the promotion gate compare `root`; equivalence (03 §3.4),
inheritance (02 §2.7) and `records-vs-origin` compare `records_root`,
which does not depend on the compressor; because the platform image pins
the compressor and its level, honest peers agree on `root` as well.

**Worked example.** Two lines. A Gutenberg record (green, text travels):

```
{"uid":"gutenberg:1342","native_id":"1342","lang":"en","title":"Pride and Prejudice","creators":["Austen, Jane"],"date":"1998-06-01","subjects":["Courtship -- Fiction","England -- Fiction"],"keys":[],"sections":[["Chapter 1","It is a truth universally acknowledged, …"],["Chapter 2","Mr. Bennet was among the earliest …"],…],"extra":{"downloads":61432},"origin":"1/3/4/1342/1342-0.txt","content_hash":"2c9e6f0b1a8d4e7f3b5c0a9d8e7f6a1b","origin_hash":"f1e2d3c4b5a69788796a5b4c3d2e1f00"}
```

A PubMed pointer record (red, text stays where it was fetched):

```
{"uid":"pubmed:38112034","native_id":"38112034","lang":"en","title":"","creators":["Rossi M","Bianchi L"],"date":"2023-12-19","subjects":["Humans","Neoplasms/genetics"],"keys":["pmid:38112034"],"sections":[],"extra":{"origin_url":"https://pubmed.ncbi.nlm.nih.gov/38112034/","revised":"2024-01-08"},"origin":"updatefiles/pubmed24n1220.xml.gz#5117","content_hash":"a3f0c1d2e3f4a5b6c7d8e9f0a1b2c3d4","origin_hash":"5e21f0a9b8c7d6e5f4a3b2c1d0e9f8a7"}
```

The pointer record's `title` is empty because a title is text; the full
record on a text-holding peer has the title and the abstract in
`sections`, and the same `content_hash`.

## 9.6 Vectors, centres and buckets

The vector files under `vectors/<space>/` are the at-rest form on one
peer: assembled from accepted bundles (§9.7), never a member's bytes,
never fetched as such — vectors travel only as bundles (04 §4.3).
`<space>` is the 16-digit segment of §9.3. Every file is safetensors;
tensor bytes are little-endian, row-major; header metadata values are
strings.

**Per-shard vectors.** `vectors/<space>/<slice>/<NNNNN>.safetensors`:

| tensor / header | type | required | meaning |
|---|---|---|---|
| `vectors` | `[n, dim]` in `space.dtype` | yes | one row per chunk of record shard `n`, in shard order: records in order, each record's chunks by `seq`; a row without an accepted vector is all zeros |
| `labels` | `[n]` u32 | yes | each row's bucket label under `index_version`; `0xFFFFFFFF` for a row with no vector |
| `chunk_ids_sha256` | header | yes | `H(id_0 ‖ id_1 ‖ …)` over the chunk ids in row order (§9.1's concatenation) |
| `producers` | header | yes | the canonical JSON array of the bundle ids merged, sorted |
| `index_version` | header | yes | the index version the labels were computed against |
| `index_min` | header | yes | the oldest index version whose centres give these labels |
| `collection_id`, `snapshot_id`, `space_id`, `shard` | header | yes | what the file belongs to; `shard` is `"<slice>/<n>"` |

The row for a chunk is the vector from the first bundle accepted for it
(04 §4.5); a later bundle for the same chunk is compared and not
stored, unless the first is flagged. Labels are computed by the
importer against the centres it holds, never read from a bundle.

**Index metadata.** `vectors/<space>/index.json`, canonical JSON, a
member (`index-<space>`, §9.16) shipped by whoever trains first:

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-index"` | yes | |
| `version` | integer | yes | the current index version; additive (02 §2.4) |
| `space_id` | id | yes | |
| `group_size` | integer | yes | labels per bucket group: `group = label // group_size` |
| `centres` | list | yes | one entry per version, ascending: `{version, k, root, bytes, additive_to: version or null, trained: {snapshot: hash, sample: integer, seed: hash, iterations: 25, init: "kmeans++", dtype: "f32", by: key, time}}` |

The training record is the rule of 02 §2.4 made exact: `k = min(⌈√n⌉,
65536)` over the `n` chunks of the snapshot; the sample is
`sample(seed, min(200000, n), all chunk ids)` (§9.1) with `seed` = the
32 raw bytes of the snapshot hash; k-means++ initialisation drawn from
the same seed; 25 Lloyd iterations in f32; a vector's label is the index
of the centre with the greatest dot product with the vector (unit
vectors), ties to the lower index. Version `v+1` keeps every centre of
`v` unchanged, in place, and appends. The `centres` check recomputes a
version from its record and compares; `labels` checks a row's label
against the centres of the version its file names.

**Centres.** `vectors/<space>/centres/v<n>.safetensors`: tensor
`centres` `[k, dim]` f32; header `index_version`, `space_id`,
`centres_sha256` = `H` over the tensor bytes. Its `root` and `bytes` are
in `index.json`.

**Bucket groups.** `vectors/<space>/buckets/v<n>/g<k>.safetensors`
(`k` zero-padded to two digits): the vectors of every chunk whose label
is in group `k` under version `n`, sorted by (label, chunk id bytes);
tensors `vectors` and `labels` as above; beside it `g<k>.ids`, the chunk
ids one per line, `\n`-terminated, zstd-compressed with the framing of
§9.5, and the header `chunk_ids_sha256` over them in row order. Local
and optional; a request for a bucket group resolves to bundles (04
§4.3).

**The text index.** `index/<slice>/<NNNNN>/` is one tantivy segment for
record shard `n`, a directory of files. It is a cache: a member
(`index-<slice>`) a peer imports only when the pin in the member's own
header — `fts_version` and `tokenizer`, written by the peer that built
it — equals what its platform builds, checks by nothing, and rebuilds from
the records otherwise. Its files are advertised with their roots by the
peer serving them (§9.9, `members`), not by the snapshot.

**Worked example.** German Wikipedia, slice `lang=de/pages=3M`, shard
`00042`, 2,000 pages, 11,806 chunks under a `cdc` layer:
`vectors/41844ebbc0d19f2e/lang=de/pages=3M/00042.safetensors` with
`vectors [11806, 1024] f16`, `labels [11806] u32`, header
`{"chunk_ids_sha256": "sha256:…", "producers": "[\"sha256:2f…\",\"sha256:9a…\"]",
"index_version": "2", "index_min": "1", "collection_id": "sha256:…",
"snapshot_id": "2026-08", "space_id": "sha256:41844ebb…", "shard":
"lang=de/pages=3M/00042"}`. Two producers, because the shard was
embedded by one peer up to chunk 7,400 and by its rendezvous successor
from there; 140 rows are zeros with label `0xFFFFFFFF` because a
bundle covering them has not yet passed this peer's ladder.

## 9.7 The bundle

A bundle is a directory of four files, the unit in which embedding work
travels (03 §3.5; 08's signed block):

```
<bundle_id hex>/
  header.json        metadata; never text, titles or authors
  vectors.parquet    chunk_id, vector[dim]; never text
  possession.json    uid → content_hash of the record the vectors were computed from
  MANIFEST.sig       exactly 64 raw bytes, Ed25519
```

On a peer, accepted bundles live at
`bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/`; a bundle is a member
named `bundle-<space>-<bundle_id hex>` (§9.16); `hold vectors export`
writes the same directory anywhere, `hold vectors import` reads it.

**header.json.** Written by the producer as JSON with sorted keys, no
whitespace, ASCII only (every non-ASCII character escaped), so the file
is byte-stable across re-exports; verified as the bytes received. Every
key below is present and no other is; strings are NFC and bounded
(header at most 1 MiB, a string at most 4,096 characters, at most 10⁶
uids); no floats anywhere.

| key | type | required | meaning |
|---|---|---|---|
| `version` | `1` | yes | |
| `collection` | id | yes | the collection the producer embedded for. Provenance, never a gate: a bundle is admissible in any collection whose `space_id` it matches (03 §3.5) |
| `space_id` | id | yes | the space, and the gate: a bundle whose `space_id` is not the importing collection's is refused at T0, before any tier runs |
| `snapshot_id` | `YYYY-MM` or `YYYY-Www` | yes | the snapshot whose records were embedded. Provenance, never a gate: an unchanged record keeps its vectors across snapshots (02 §2.2), and `possession.json` — not this field — decides whether a row still matches the text |
| `shard` | string | yes | `"<slice>/<n>"` — the record shard the chunks come from; one shard per bundle |
| `uids` | list of strings | yes | the uids whose chunks the bundle covers, every chunk of each, sorted by bytes, no duplicates: the bundle's chunk range |
| `chunk_count` | integer | yes | rows in `vectors.parquet` after the challenge rows |
| `dim` | integer | yes | |
| `dtype` | `"f16"` · `"f32"` | yes | the parquet element type |
| `producer_pubkey` | key | yes | the signer; `ed25519:…` |
| `encoder_image`, `encoder_weights` | id | yes | the producer's encoder build; equal to the Codex's, or a build attested compliant (03 §3.3) |
| `content_digest` | id | yes | the container-independent digest of the rows, below |
| `parquet_bytes`, `possession_bytes` | integer | yes | sizes of the two files |
| `created_at` | integer | yes | Unix seconds |
| `digest_alg` | `"sha256"` | yes | compared against the literal; never dispatched |
| `sig_alg` | `"ed25519"` | yes | likewise |
| `challenge` | object | yes | `{set: id, n: 16}` — the T2 challenge: the first `n` rows of `vectors.parquet` are the calibration set's first `n` texts (§9.12) embedded by the producer, with `chunk_id` `"calibration:<i>"`, `i` from 0 |

**vectors.parquet.** Two columns, `chunk_id` (string) and `vector`
(fixed-size list of `dim` elements of `dtype`); the challenge rows first
in index order, then the chunk rows sorted by (uid UTF-8 bytes, `seq` as
an integer) — a tuple sort, under which `:10:` follows `:2:`. Parquet
bytes are not stable across writers, so the signature covers the file's
SHA-256 (this file, as received) and the header's `content_digest`
covers the content:

```
content_digest = sha256( "louise-rag/vectors/v1" ‖ 0x00
                         ‖ u32be(dim) ‖ u32be(rows)
                         ‖ for each row in file order: u32be(len(chunk_id)) ‖ chunk_id ‖ vector as big-endian dtype bytes )
```

written `sha256:…`.

**possession.json.** A JSON object, sorted keys, ASCII, mapping each uid
in `uids` to the `content_hash` of the record whose text the producer
embedded. The importer compares every entry with its own record; one
mismatch refuses the bundle at T0 — vectors of another revision never
attach to this text. Read only after the signature verifies; its digest
is inside the signed payload.

**The signed payload and the id.**

```
payload   = "louise-rag/bundle-sig/v1" ‖ 0x00
            ‖ u32be(len(header_bytes)) ‖ header_bytes
            ‖ u32be(32) ‖ sha256(vectors.parquet bytes)
            ‖ u32be(32) ‖ sha256(possession.json bytes)
MANIFEST.sig = Ed25519(producer key, payload)          -- 64 raw bytes
bundle_id    = "sha256:" ‖ hex(sha256(payload))
```

The id is derived from the signed bytes, so it cannot disagree with
what was signed and is not a header field; a producer that re-emits the
same bundle produces the same id. `verify` proves that whoever holds
`producer_pubkey` signed exactly these bytes; whether the vectors are
any good is the ladder's question (03 §3.5), and whether the key may be
counted is the trust file's (§9.2).

**The bundle event** that announces it names `collection`, `space_id`,
`snapshot_id`, `shard`, `uids` range as `[first, last]`, `bundle_id`,
`root` and `bytes` (§9.9). A `withdraw` attestation by the producer
(§9.8) names bundle ids the producer retracts; peers drop them, no key
is burned.

**Worked example.** A Gutenberg bundle, slice `lang=it`, shard
`00007`, the first 300 chunks a contributor embedded before its first
partial publication:

```
header.json:
{"challenge":{"n":16,"set":"sha256:6b1a…"},"chunk_count":300,"collection":"sha256:e5c2…","content_digest":"sha256:0f7b…","created_at":1756713600,"digest_alg":"sha256","dim":1024,"dtype":"f16","encoder_image":"sha256:8c…","encoder_weights":"sha256:e2…","parquet_bytes":662148,"possession_bytes":1417,"producer_pubkey":"ed25519:19…","shard":"lang=it/00007","sig_alg":"ed25519","snapshot_id":"2026-08","space_id":"sha256:41844ebb…","uids":["gutenberg:10102","gutenberg:10125","gutenberg:1012","gutenberg:10131",…],"version":1}
possession.json:
{"gutenberg:1012":"7a1c…","gutenberg:10102":"bb02…",…}
```

`uids` is sorted by bytes, which is why `gutenberg:1012` follows
`gutenberg:10125`; the parquet's first sixteen rows are
`calibration:0` … `calibration:15`, then `gutenberg:1012:0:1`,
`gutenberg:1012:1:1`, ….

## 9.8 Attestations

An attestation is the signed result of one completed check (03 §3.6),
stored at `state/attestations/<collection_id hex>/<attestation id
hex>.json` as an envelope (§9.2) and carried by an `attestation` event
(§9.9). Its id is `H(canonical JSON of body)`. A check that could not
run — *inconclusive* (03 §3.1) — produces no attestation.

**The body.**

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-attestation"` | yes | |
| `version` | `1` | yes | |
| `collection_id` | id | yes | |
| `snapshot_id` | `YYYY-MM` or `YYYY-Www` | yes | |
| `snapshot_hash` | id | yes | which of two snapshots under one id was checked |
| `check` | string | yes | the check, below |
| `seed` | 64 hex or null | yes | the 32-byte seed of §9.1's sampler; `null` for an exhaustive or deterministic check |
| `shards` | list of strings | yes | the shards the check ran on, as `"<collection_id>/<space>/<slice>/<n>"` for a vector check and `"<collection_id>/<slice>/<n>"` for a record check, sorted; `[]` for a check without shards |
| `result` | `"pass"` · `"fail"` · `"forgery"` · `"withdraw"` · `"supersedes"` | yes | |
| `detail` | object | yes | per check, below; `{}` when the check has none |
| `platform_image` | id | yes | the image the verifier ran; when it differs from the Codex's and `check` is `records-vs-origin` with `result: "pass"`, the attestation is the equivalence proof of 03 §3.4 |
| `encoder_image`, `encoder_weights` | id or null | yes | the encoder used, for a statistical check; `null` otherwise |
| `key` | key | yes | the verifier |
| `time` | time | yes | |

**Checks and their detail.**

| check | kind | detail |
|---|---|---|
| `roots` | exact | `{failed: [shard…]}` |
| `records` | exact | `{records: n, failed: [uid…]}` — content and origin hashes present and, where the text is, reproduced |
| `chunks` | exact | `{records: n, failed: [uid…]}` |
| `records-vs-origin` | exact | `{records: n, failed: [uid…], origin: "reachable"}`; a failing entry names the first differing record |
| `vectors-sample` | statistical | `{chunks: n, passed: n, median: decimal, failed: [chunk id…], producers: [key…]}` — the producers whose bundles supplied the sampled vectors; two producers when the attestation compares duplicate bundles (04 §4.5) |
| `calibration` | statistical | `{n: 16, passed: n, median: decimal}` |
| `centres` | exact | `{index_version: n}` |
| `labels` | exact | `{index_version: n, failed: [chunk id…]}` |
| `license` | exact | `{}` |
| `signature` | exact | `{keys: [key…]}` — the sidecar signatures that verified |
| `anchor` | exact | `{state: "anchored" · "stale" · "mismatch"}` |
| `origin-seal` | exact | `{mode: "live" · "archival", entries: n, failed: [file_hash…]}` |
| `evaluation` | statistical | `{suite: {name, version, sha256}, metric, expected: decimal, observed: decimal, targets: n, resolved: n}` — the score `space.evaluation` claims, re-run on this copy; `resolved` is how many of the suite's targets this copy holds, and a run below the suite's own coverage floor is `result: "fail"` with `observed: null` |
| `merge` | exact | `{other: collection_id}` — two daughters' snapshots have one hash (04 §4.6); `snapshot_hash` is that hash |
| `forgery` | proof | `{chunk_id, producer: key, bundle: id, claimed: id, recomputed: id, text: string or null, content_hash: BLAKE2b-128}` — `claimed` and `recomputed` are `H` over the vector as big-endian `space.dtype` bytes; `text` is the chunk's text, or `null` on a red-class collection, whose `content_hash` stands in for it |
| `withdraw` | producer's own | `{bundles: [id…]}` — the signer retracts its own bundles; `result: "withdraw"` |
| `supersedes` | claim | `{superseded: collection_id, evaluation: <the Codex's evaluation block>}` — a sister claims a better score; `result: "supersedes"` |

A failing statistical attestation lists the failing chunk ids in
`detail.failed`; a second key that re-embeds exactly those ids and fails
flags the collection (03 §3.6). Attestations older than two cadences do
not count toward a level.

**What `hold check --json` prints.** `{collection_id, snapshot_id,
snapshot_hash, checks: [{check, result: "pass" · "fail" ·
"inconclusive", detail, attestation: id or null}]}`; `attestation` is
the id of the attestation the run produced, `null` when inconclusive.

**Worked example.** A peer re-embeds sixteen chunks of one PubMed shard
and finds a second producer's bundle compliant with the first:

```
{"body":{"check":"vectors-sample","collection_id":"sha256:9d3e…41ab",
         "detail":{"chunks":16,"failed":[],"median":"0.9987","passed":16,"producers":["ed25519:19…","ed25519:77…"]},
         "encoder_image":"sha256:8c…","encoder_weights":"sha256:e2…","key":"ed25519:c3…","kind":"louise-attestation",
         "platform_image":"sha256:1f…","result":"pass","seed":"9b4e…d201",
         "shards":["sha256:9d3e…41ab/41844ebbc0d19f2e/year=2023/00011"],
         "snapshot_hash":"sha256:c07d…","snapshot_id":"2026-08","time":"2026-09-04T22:15:09Z","version":1},
 "sig":"…"}
```

## 9.9 Digests, events and peer messages

Peers speak five messages, every one a signed envelope (§9.2) carried
as the body of an HTTP `POST` under `/api/share/` (07 §7.4): the sender
is identified by `body.key` and nothing else. A receiver checks the
signature before reading a field, drops a message whose `time` is more
than one interval in the future, and answers a pull with self-verifying
objects, which are therefore not signed again.

| message | route | body kind | domain |
|---|---|---|---|
| digest | `POST /api/share/digest/share` | `louise-digest` | `louise/digest/v1` |
| event | `POST /api/share/event/share` | `louise-event` | `louise/event/v1` |
| pull codex | `POST /api/share/codex/fetch` | `louise-pull` | `louise/event/v1` |
| pull snapshot | `POST /api/share/snapshot/fetch` | `louise-pull` | `louise/event/v1` |
| pull attestations | `POST /api/share/attestations/fetch` | `louise-pull` | `louise/event/v1` |

**The digest.** Sent hourly to every neighbour and to every address in
the peer list (04 §4.2).

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-digest"` | yes | |
| `version` | `1` | yes | |
| `key` | key | yes | the sender |
| `time` | time | yes | |
| `address` | string | yes | where the sender answers pulls: `https://host:port` |
| `neighbours` | list of `{key, address}` | yes | up to sixteen live neighbours |
| `benchmarks` | list | yes | `{space_id, encoder_image, encoder_weights, tokens_per_s: integer}`, one per space this peer holds a passing calibration statement for (03 §3.3). Keyed by space because that is how 04 §4.5 reads it: a peer with no entry for a collection's space is unscored for that collection |
| `collections` | list | yes | one entry per collection held, below |

A collection entry:

| field | type | required | meaning |
|---|---|---|---|
| `collection_id`, `lineage_id` | id | yes | |
| `title` | string | yes | the `descriptive.title` of the Codex this peer holds, so a receiver can name a collection it has not fetched; untrusted, never used to sort, merge or resolve (08 §8.2) |
| `snapshot_id`, `snapshot_hash` | | yes | the latest snapshot held, and its hash |
| `codex_hash` | id | yes | `H` of the bytes of the `codex.json` THIS peer holds (§9.3). A `codex.json` pulled from this peer whose bytes hash otherwise is dropped: the pull path answers with stored bytes and no signature (below), so this is what makes it self-verifying. It is per-peer and not an identity — two honest peers may hold the same `collection_id` under different titles, hence different `codex_hash` (02 §2.1) — so it is compared only against the digest that advertised it |
| `license`, `privacy` | string | yes | the Codex's classes, so a receiver can skip what it may not take |
| `members` | list of `{name, root, bytes}` | yes | every member offered, by name (§9.16); a record shard's root is the snapshot's, a bundle's root is its member root |
| `coverage` | list of `{shard, bundles: [id…]}` | yes | per shard `"<collection_id>/<space>/<slice>/<n>"`, the bundle ids this peer has accepted through T0–T3, sorted |

A `restricted` collection appears only in digests sent to keys on the
sender's allow list; a `never-share` collection appears in none.

**The event.** Forwarded once; the hop count sits outside the signed
body:

```
{"body": {…}, "sig": "…", "hops": 8}
```

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-event"` | yes | |
| `version` | `1` | yes | |
| `key` | key | yes | the originator |
| `time` | time | yes | |
| `type` | `"snapshot"` · `"codex"` · `"attestation"` · `"flag"` · `"bundle"` | yes | |
| `payload` | object | yes | by type, below |

| type | payload |
|---|---|
| `snapshot` | `{collection_id, snapshot_id, snapshot_hash, sig: {key, time, sig}}` — the announcer's own sidecar entry |
| `codex` | `{collection_id, chain: [<Codex object>…]}` — the new Codex first, then the Codexes it comes from along `continues`, `derived_from` and `replaces` back to the root: a daughter, a sister or a corrective sister announced |
| `attestation` | the attestation envelope (§9.8) |
| `flag` | `{collection_id, snapshot_hash, check, attestations: [id…]}` — the failing attestations that together flag it (03 §3.6) |
| `bundle` | `{collection, space_id, snapshot_id, shard, uids: [first, last], bundle_id, root, bytes}` |

The event id is `H(canonical JSON of body)`, computed by the receiver;
an event carries no id field, so none can be forged. The receiver keeps
ids seen for seven days, drops an event older than that or already
seen, decrements `hops` and forwards to its neighbours while `hops > 0`.

**The pull.**

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-pull"` | yes | |
| `version` | `1` | yes | |
| `key`, `time` | | yes | |
| `what` | `"codex"` · `"snapshot"` · `"attestations"` | yes | |
| `collection_id` | id | yes | |
| `snapshot_id` | `YYYY-MM` or `YYYY-Www`, or null | yes | for `snapshot`; `null` means the latest the responder holds |
| `since` | time or null | yes | for `attestations`: only those with `time` after it |

The response is one JSON object `{"items": [{"path": string, "sha256":
id, "b64": string}…]}` — each item the exact stored bytes, base64 —
never a re-serialisation: `codex.json` for the collection and for every
Codex in its chain (`path` is `codex/<collection_id hex>.json`);
`snapshots/<snapshot_id>.json` and `snapshots/<snapshot_id>.sig.json`; attestation
envelopes as `attestations/<id hex>.json`. The receiver hashes each
item's bytes, compares with `sha256` and with what a digest advertised,
and treats a mismatch as 04 §4.2 says.

**Worked example.** A digest entry for the Gutenberg collection held by
a peer that serves Italian and English and has accepted one bundle on
shard 7:

```
{"collection_id":"sha256:e5c2…","lineage_id":"sha256:e5c2…","snapshot_id":"2026-08","snapshot_hash":"sha256:70a1…",
 "license":"green","privacy":"public",
 "members":[{"name":"records-lang=it","root":"sha256:…","bytes":…},{"name":"records-lang=en","root":"sha256:…","bytes":…},
            {"name":"bundle-41844ebbc0d19f2e-2f0c…","root":"sha256:…","bytes":663629},
            {"name":"index-41844ebbc0d19f2e","root":"sha256:…","bytes":2211}],
 "coverage":[{"shard":"sha256:e5c2…/41844ebbc0d19f2e/lang=it/00007","bundles":["sha256:2f0c…"]}]}
```

## 9.10 The coverage map (local)

The coverage map is each peer's own view (02 §2.5): never a member,
never signed, never the same on two peers. It lives in the state
database (`state/manifest.sqlite`, 07 §7.8) as one row per accepted
bundle and is printed by `hold vectors show --json`.

| field | type | meaning |
|---|---|---|
| `shard` | string | `"<collection_id>/<space>/<slice>/<n>"` — the rendezvous `shard_id` (04 §4.5) |
| `bundle_id` | id | |
| `producer` | key | |
| `uids` | `[first, last]` | the bundle's chunk range |
| `chunks` | integer | rows merged from it |
| `tiers` | object | `{T0, T1, T2, T3}` each `"pass"` · `"fail"` · `"skipped"`; T1 is skipped on a non-`fixed` layer, T1, T3 on a peer without the text |
| `accepted_at` | time | when it passed this peer's ladder; `null` while it has not |
| `source` | `"local"` · `"fetched"` · `"archive"` | who produced the vectors from this peer's view |
| `verified_on_use` | integer | chunks of it T4 has re-embedded and confirmed |

Derived from the rows, per shard: `covered` — chunks with an accepted
row; `open` — the shard has any chunk without one; the leader and the
grace window of 04 §4.5. A coverage claim in a neighbour's digest is
recorded beside these as `claimed_by: [key…]` per bundle and counts for
nothing until the bundle is fetched and accepted here.

**What `hold vectors show --json` prints.** `{collection_id, space_id,
snapshot_id, chunks, covered, shards: [{shard, chunks, covered, open,
leader: key or null, window_ends: time or null, bundles: [<rows as
above>]}], producers: [{key, chunks, bundles, tokens_per: {per: duration,
tokens: integer}}], vitality: "actively-held" · "dormant"}` — the
last two are the computation of 04 §4.7, which reads this map for
bundles and the
attestations this peer holds (§9.8) for checks, since signed work of
either kind counts and only one of the two is recorded here.

**Worked example.** PubMed, `year=2023`, shard 11, on a peer that
embedded half of it before its successor took over:

```
{"shard":"sha256:9d3e…41ab/41844ebbc0d19f2e/year=2023/00011","chunks":30000,"covered":30000,"open":false,
 "leader":null,"window_ends":null,
 "bundles":[{"bundle_id":"sha256:2f0c…","producer":"ed25519:19…","uids":["pubmed:37000021","pubmed:37019994"],"chunks":14800,
             "tiers":{"T0":"pass","T1":"skipped","T2":"pass","T3":"pass"},"accepted_at":"2026-09-02T09:11:03Z","source":"local","verified_on_use":211},
            {"bundle_id":"sha256:9a41…","producer":"ed25519:77…","uids":["pubmed:37020007","pubmed:37041560"],"chunks":15200,
             "tiers":{"T0":"pass","T1":"skipped","T2":"pass","T3":"pass"},"accepted_at":"2026-09-05T17:48:30Z","source":"fetched","verified_on_use":48}]}
```

T1 is skipped because PubMed's layer is `abstract`, not `fixed`.

## 9.11 The certifier's pointer and anchor document

**The anchor document.** `https://<domain>/.well-known/louise-certifier.json`,
fetched over TLS; the domain's TLS chain is the anchor and the document
carries no signature of its own.

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-certifier"` | yes | |
| `version` | `1` | yes | |
| `keys` | list of `{key, valid_from: time, revoked_at: time or null}` | yes | every key the certifier has used, oldest first |
| `collections` | list of ids | yes | the certified collections this certifier stands behind |

A peer fetches it at adoption and every 24 hours with a jitter of up to
four hours; unreachable → the last state is kept and shown as `stale`
after 30 days; a Codex `certifier.key` not in `keys`, or a
`collection_id` not in `collections` → `mismatch`, and the collection is
shown as open, published by a key that claims to be the certifier
(03 §3.7). A key certifies snapshots whose sidecar time is at or after
its `valid_from`; a `revoked_at` invalidates the signatures dated after
it that no other valid key also made.

**The pointer.** Body signed under `louise/pointer/v1`:

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-pointer"` | yes | |
| `version` | `1` | yes | |
| `collection_id` | id | yes | |
| `snapshot_id` | `YYYY-MM` or `YYYY-Www` | yes | the current certified snapshot |
| `snapshot_hash` | id | yes | |
| `seq` | integer | yes | strictly increasing per collection; a peer keeps the highest verified `seq` and discards lower ones |
| `key` | key | yes | the certifier's key |
| `time` | time | yes | |

It is published twice with the same bytes: as a BEP 44 mutable item
under the certifier's Ed25519 key with `salt` = the 32 raw bytes of
`collection_id`, `seq` = `seq`, and `v` = the envelope's canonical
bytes; and at
`https://<domain>/.well-known/louise-pointer/<collection_id hex>.json`,
the fallback when the DHT is unreachable. The BEP 44 signature is the
DHT's own, over its `salt ‖ seq ‖ v`; the envelope's `sig` is the one a
peer verifies against the Codex.

**Worked example.** The Gazzetta Ufficiale's anchor at
`https://www.gazzettaufficiale.it/.well-known/louise-certifier.json`
lists `{key: "ed25519:4a…", valid_from: "2026-01-01T00:00:00Z",
revoked_at: null}` and the collection `sha256:0c8f…`; its pointer for
the August snapshot is `{body: {kind: "louise-pointer", version: 1,
collection_id: "sha256:0c8f…", snapshot_id: "2026-08", snapshot_hash:
"sha256:c07d…", seq: 20, key: "ed25519:4a…", time:
"2026-09-01T06:13:00Z"}, sig: "…"}`, stored in the DHT under
`sha1(pubkey ‖ salt)` as BEP 44 defines and served at
`/.well-known/louise-pointer/0c8f….json`.

## 9.12 The calibration member

**The set.** A calibration set is a corpus-independent file of 512
short texts shipped with the platform, `calibration/<set>.jsonl` in
the platform image — one line per text, `{"i": <0…511>, "text": "…"}`,
compact, `\n`-terminated. Its id is `H(bytes of the file)`; `<set>` in
a path is the id's 16-digit segment. The platform ships one set per
modality; the Codex names the one it uses in `space.calibration.set`.

**The member.** `calibration/<set>.safetensors` at the drag root,
written by `hold collection create` (06 §6.6) and a member of every
snapshot (`files`, §9.4):

| tensor / header | type | required | meaning |
|---|---|---|---|
| `vectors` | `[512, dim]` f32 | yes | the reference vectors, in set order, as the creator's encoder produced them, unit-normalised when `space.normalise` |
| `envelope` | `[2, dim]` f32 | yes | row 0 the per-dimension mean of `vectors`, row 1 the per-dimension standard deviation |
| `set` | header | yes | the set's id |
| `encoder_image`, `encoder_weights` | header | yes | the creator's encoder build |
| `key` | header | yes | the creator's key |

`space.calibration.vectors` = `H` over the bytes of the `vectors`
tensor; `space.envelope` = `H` over the bytes of the `envelope` tensor;
a member whose tensors hash otherwise is refused. Beside it,
`calibration/<set>.sig.json` is an envelope signed under
`louise/calibration/v1` with body `{kind: "louise-calibration", version:
1, set, space_id, vectors: id, envelope: id, encoder_image,
encoder_weights, key, time}` — who produced the reference, for
provenance; the Codex's hashes, not the signature, are what bind it.

**Three uses, one file.** `hold embedder check` embeds the 512 texts,
draws `quorum.n` of them by `sample(seed, n, {"0"…"511"})` with the
peer's own seed, and passes when at least `quorum.min_pass` reach cosine
≥ `tolerance` against `vectors` and the median does; the same run
measures the benchmark in tokens per second (03 §3.3). T1's probe set is
`vectors`; T0's envelope is `envelope`; T2's golden pairs are texts
0–15 with their rows (§9.7). Two sisters on one encoder share one
calibration member byte for byte (08 §8.3). Cosines are computed in f32
after upcasting.

**The peer's calibration statement.** The member above is the space
author's reference. What a contributing peer produces when it passes
the gate (03 §3.3) is a different object, and the one every other
document means by "a compliant encoder": an envelope (§9.2) under the
domain `louise/calibration/v1`, kept in `state/` and published in this
peer's digest (§9.9), with body

```
{kind: "louise-calibration-run", version: 1,
 space_id, collection_id, set,
 encoder_image, encoder_weights,
 seed, sampled: [<integer 0…511>…], passed: <integer>,
 worst: <decimal>, median: <decimal>,
 max_input_tokens: <integer>, tokens_per_s: <integer>,
 key, time}
```

`sampled` is the `quorum.n` texts the sampler of §9.1 drew with `seed`;
`passed` is how many reached `tolerance`; `worst` and `median` are the
cosines over them. `max_input_tokens` is what the endpoint accepted, so
a verifier knows not to sample a chunk this peer could not have
embedded whole, and `tokens_per_s` is the benchmark §4.5 weights
rendezvous by. A statement is current while its `encoder_image`,
`encoder_weights` and `set` are the ones in use; changing any of the
three needs a new run (03 §3.3).

**Worked example.** The text set shipped as `calibration/6b1a3f0e9c2d4b7f.jsonl`
(`sha256:6b1a3f0e9c2d4b7f…`); the PubMed Codex names it, and its member
`calibration/6b1a3f0e9c2d4b7f.safetensors` holds `vectors [512, 768]`
and `envelope [2, 768]` with `space.calibration.vectors: "sha256:d4…"`
and `space.envelope: "sha256:77…"`. A sister of PubMed on a
domain-trained encoder names the same set and a different member.

## 9.13 Licence terms and the acknowledgement

**The terms member.** `LICENSE-TERMS` at the drag root: the origin's
terms text as published, byte-exact, never converted — the repository
marks the source file binary so no checkout changes a line ending
(06 §6.1). `license.terms_sha256` = `H(bytes of LICENSE-TERMS)`; a
green collection has `terms_sha256: null` and no member. Before an
acknowledgement a peer may fetch exactly `codex.json`, `snapshots/*` and
`LICENSE-TERMS` (04 §4.4).

**The acknowledgement.** `state/ack.json` in the collection's state
directory (07 §7.8), local, never a member:

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-ack"` | yes | |
| `version` | `1` | yes | |
| `terms_sha256` | id | yes | the hash of the text acknowledged |
| `time` | time | yes | when |
| `collection_id` | id | yes | the collection through which it was recorded |

An acknowledgement is of a text: every gate compares `terms_sha256` with
`H` of the `LICENSE-TERMS` member present now, through one resolution
(`app/licensing.py`), and a collection whose Codex names a hash this
peer has acknowledged under any collection is acknowledged.

**Worked example.** Wikipedia: `LICENSE-TERMS` is the CC BY-SA 4.0
legal code as Wikimedia publishes it, `sha256:8e4a…`; after `hold
collection ack wikipedia-de`, `state/ack.json` reads `{"kind":
"louise-ack","version":1,"terms_sha256":"sha256:8e4a…","time":
"2026-09-02T08:00:41Z","collection_id":"sha256:b7…"}`, and the French
Wikipedia collection, whose Codex names the same hash, is acknowledged
by it.

## 9.14 Relations

**Path.** `relations/<slice>/<NNNNN>.jsonl.zst`, one shard beside each
record shard of the same slice and number (05 §5.5), listed in the
snapshot's `relations` with its `root` (§9.4) and re-issued with its
record shard. The file uses the framing of §9.5 with the index's `uids`
map keyed by `from` (the frame and line of the first edge of each
record).

**The edge line.** Compact JSON, keys in this order, `\n`-terminated:

| key | type | required | meaning |
|---|---|---|---|
| `from` | uid | yes | a record of this collection, in this shard |
| `to` | string | yes | a uid of this collection, or a canonical id `<namespace>:<id>` left unresolved |
| `type` | string | yes | `<plugin>:<name>`, registered by the plugin: `eli:cites`, `eli:amends`, `eli:repeals`, `pubmed:cites` |
| `snapshot` | `YYYY-MM` or `YYYY-Www` | yes | the snapshot in which the edge was first listed |

Edges are sorted by (`from` bytes, `type` bytes, `to` bytes); the
direction is from → to as the source states it.

**Worked example.** The Gazzetta Ufficiale, slice `year=2024`, the
decree that amends decree 81/2008:

```
{"from":"gazzetta:eli-id-2024-03-12-31","to":"gazzetta:eli-id-2008-04-09-81","type":"eli:amends","snapshot":"2026-08"}
{"from":"gazzetta:eli-id-2024-03-12-31","to":"eli:eu:dir:2019:1937","type":"eli:cites","snapshot":"2026-08"}
```

The second edge points outside the collection at a European directive
and stays unresolved until the asking peer names the collection to
resolve it against.

## 9.15 Seals

**Path.** `seals/<snapshot_id>.json` at the drag root, certified
collections only (03 §3.7); canonical JSON; its transport root and size
are in the snapshot's `files`, which the certifier signs, so the
evidence is bound to the snapshot it was captured for.

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-seals"` | yes | |
| `version` | `1` | yes | |
| `collection_id`, `snapshot_id` | | yes | |
| `entries` | list | yes | one per sealed origin file, sorted by `file_hash` |

An entry:

| field | type | required | meaning |
|---|---|---|---|
| `file_hash` | BLAKE2b-128 | yes | the sealed file's hash. A record parsed from it carries that hash in `extra.seal` (§9.5), which is how it finds its entry: a record's own `origin_hash` covers the document alone (02 §2.3), so the two are different values and the reference is explicit |
| `format` | `"cades"` · `"pades"` · `"xades"` | yes | the seal's envelope |
| `seal` | `{b64: string}` | yes | the signature envelope itself, DER as base64 — the CMS `SignedData` for CAdES and PAdES, the `<ds:Signature>` element for XAdES |
| `content_sha256` | id | yes | the message digest the signature commits to, taken from the envelope's signed attributes. Archival mode verifies the signature over `seal` and checks it commits to this value; it is NOT `file_hash`, which is BLAKE2b-128 of the sealed file (below) and is what live mode re-fetches against |
| `signer` | object | yes | `{subject, issuer, serial}` of the signing certificate, as strings |
| `chain` | list of strings | yes | the certificate chain, DER as base64, leaf first |
| `revocation` | list of `{kind: "ocsp" · "crl", b64: string, time}` | yes | the responses captured at validation |
| `timestamp` | `{b64: string, time}` or null | yes | an RFC 3161 token over the sealed bytes, when one was obtained |
| `trusted_list` | `{lotl_sha256: id, tl_sha256: id, url: string}` | yes | the List of Trusted Lists and the national list the chain validated against, hashed |
| `verified_at` | time | yes | when the certifier validated it |

`origin-seal` in archival mode verifies the signature in `seal` over
the digest in `content_sha256` and validates its certificate path
against `chain`, `revocation` and `trusted_list`, with no network and
without the sealed bytes; in live mode it
re-fetches the sealed file, checks it hashes to `file_hash`, and
validates against the current lists. The sealed files themselves travel
only in the optional member `origin-<slice>` (§9.16), never in the
records.

**Worked example.** One entry for the Gazzetta issue of 12 March 2024,
whose `.p7m` hashes to `4e1a…` and whose acts are the records carrying
that `file_hash`: `{"file_hash":"4e1a…","format":"cades","signer":
{"subject":"CN=…, O=Istituto Poligrafico e Zecca dello Stato",…},
"chain":["MIIG…","MIIF…"],"revocation":[{"kind":"ocsp","b64":"MIIB…",
"time":"2026-09-01T06:10:12Z"}],"timestamp":{"b64":"MIIC…","time":
"2026-09-01T06:10:14Z"},"trusted_list":{"lotl_sha256":"sha256:…",
"tl_sha256":"sha256:…","url":"https://eidas.agid.gov.it/TL/TSL-IT.xml"},
"verified_at":"2026-09-01T06:10:15Z"}`.

## 9.16 Members and torrents

**Member names.** A member is one unit a peer fetches or shares
(02 §2.6). Its name is fixed by its kind:

| name | files | root |
|---|---|---|
| `records-<slice>` | every `records/<slice>/<NNNNN>.jsonl.zst` of the current snapshot | per shard, in the snapshot |
| `relations-<slice>` | `relations/<slice>/*.jsonl.zst` | per shard, in the snapshot |
| `index-<slice>` | `index/<slice>/<NNNNN>/*` — the text-index segments | per file, in the digest |
| `bundle-<space>-<bundle_id hex>` | the four bundle files | member root, in the bundle event and the digest |
| `vectors-<space>-<slice>`, `buckets-<space>-v<n>-g<k>` | names only: resolve to the bundles covering those chunks (04 §4.3) | — |
| `centres-<space>-v<n>` | `vectors/<space>/centres/v<n>.safetensors` | in `index.json` |
| `index-<space>` | `vectors/<space>/index.json` | in the digest |
| `calibration-<set>` | `calibration/<set>.safetensors` and `.sig.json` | in the snapshot's `files` |
| `validation` | `validation.json` | in the snapshot's `files` |
| `terms` | `LICENSE-TERMS` | in the snapshot's `files` |
| `seals-<snapshot_id>` | `seals/<snapshot_id>.json` | in the snapshot's `files` |
| `origin-<slice>` | the sealed origin files of a certified collection, optional | per file, in the digest |

A member of one file has that file's pieces root as its `root` (§9.5).
A member of several files has a root per file, and its member root is
`H` over the canonical JSON of the list `[[path, root, bytes], …]` in
path byte order; the digest lists the files.

**Torrents.** Every file of a member is one BitTorrent v2 torrent
(BEP 52), and the torrent is a function of the file's path, size and
root alone, so two honest publishers produce one infohash. The info
dictionary contains exactly: `meta version: 2`, `name` = the member
name, `piece length: 16777216`, and `file tree` with the one file at its
drag-relative path carrying `length` and `pieces root`; no `private`
flag, no comment, no creation date, no creator, no announce list — the
swarm is found through the DHT and the peer messages. The infohash is
SHA-256 of the bencoded info dictionary, truncated to 20 bytes where the
DHT needs 20; the piece layers are exchanged in the swarm. A member of
several files is that many torrents. Nothing is served from a torrent
that is not in the drag, in `state/bundles/` (§9.7), or in the
platform's encoder store (04 §4.3), and the same key that signs digests
seeds them.

**DHT keys.** The mainline DHT (BEP 5). A collection's key for a
snapshot is the first 20 bytes of `H(collection_id ‖ snapshot_id)`; the
lineage key is the first 20 bytes of `H(lineage_id ‖ snapshot_id)`; an
encoder's key is the first 20 bytes of `H` of its weights digest, under
which any peer holding those weights announces, whatever collections it
holds (04 §4.3); a
sharer announces its peer-message port under both for every snapshot it
serves (04 §4.2). What a searcher finds there is an address that speaks
§9.9.

**Worked example.** PubMed, snapshot `2026-08`, member
`records-year=1975-1979`: seven files `records/year=1975-1979/00001.jsonl.zst`
… `00007.jsonl.zst`, each a torrent whose info dictionary is
`{"file tree": {"records": {"year=1975-1979": {"00001.jsonl.zst": {"":
{"length": 10485760, "pieces root": <32 bytes 0be4…>}}}}}, "meta
version": 2, "name": "records-year=1975-1979", "piece length":
16777216}`; the DHT key under which the collection's sharers are found
is the first 20 bytes of `SHA-256(["sha256:9d3e…41ab","2026-08"])`.

## 9.17 The acquisition spec and plugin types

**The spec, in two halves** (06 §6.2). The identifying half is
serialised into the Codex's `origin.spec` in canonical JSON; the local
half never leaves the plugin and the peer.

| field | type | in the Codex | meaning |
|---|---|---|---|
| `source_id` | string | yes | the plugin |
| `transport` | `"https"` · `"rsync"` · `"ftp"` · `"hf_hub"` · `"torrent"` | yes | |
| `url` | string | yes | the origin's own base — never a mirror |
| `layout` | `"one-dir-per-doc"` · `"container-files"` · `"single-file"` | yes | |
| `listing` | `"directory"` · `"static"` · `"snapshot-index"` | yes | how the complete listing is enumerated: the transport's directory listing, a declared file list, or the origin's index of dated dumps |
| `files` | list of `{rel_path, size or null, checksum or null}` or null | yes | the declared list when `listing` is `"static"`; for a dated-dump origin the dump pinned |
| `filters` | list of strings | yes | the base include patterns |
| `checksums` | `{kind: "sidecar" · "manifest" · "none", suffix, algo}` | yes | |
| `delta` | `{mode: "updatefiles-dir" · "dated-dumps" · "full-resync", subdir: string or null}` | yes | |
| `endpoints` (mirrors) | | no | additional endpoints, regions, connection limits |
| `etiquette` | | no | `{max_conns, bandwidth_cap_bps, windows}` |
| `profiles`, `profile_mode`, `profile_labels`, `estimated_bytes` | | no | named selections of slices and their sizes |
| `probe_file` | | no | the mirror structure probe |
| `extras` | | no | formats the origin offers that Louise does not exchange |
| `terms_text_path`, `ssh_key_path`, `auth_token_path` | paths | no | never in any shared object |

**Plugin types** (06 §6.1).

`DocRef`

| field | type | required | meaning |
|---|---|---|---|
| `source_id` | string | yes | |
| `native_id` | string | yes | |
| `locator` | string | yes | the origin locator; becomes the record's `origin` |
| `extra` | object | yes | discovery-time hints, consumed and discarded; `reason` on a deletion |
| `deleted` | boolean | yes | the origin retracted the document |
| `mtime`, `size` | integer or null | yes | listing facts, when known |

`RawDoc`: `{ref: DocRef, bytes: bytes, origin_hash: BLAKE2b-128}` — one
document's origin bytes and their hash. A plugin that streams a
container returns the member's bytes and the member's hash: the unit of
verification is the document, never the artefact it was shipped in
(02 §2.3, 06 §6.4).

`DiscoverState`: `{corpus_root: path, cursor: object, committed(uid) →
boolean}` — `cursor` is the plugin's own resumable state as the platform
last saved it, a JSON object stored at `state/cursor.json`; the platform
saves it only after the manifest commits.

`CanonicalDoc`: the fields of 06 §6.1, in the record's order — `uid`,
`title`, `creators`, `language`, `date`, `subjects`, `keys`, `extra`,
`sections`, `origin_hash`; the platform adds `lang` (from `language`),
`origin` (from the `DocRef`) and `content_hash` when it writes the
record (§9.5). It adds no layer: which layer a document falls in is
derived from the Codex's rules whenever it is needed, never stored
(02 §2.3).

`Edge`: `{to: str, type: str}` — what `relations()` yields, one call
per parsed document (06 §6.1). `to` is the uid the edge points at, which
this collection need not hold; `type` is the plugin's own name for the
kind of edge. The platform adds `from`, the document's own uid, and
`snapshot`, which the plugin cannot know, and writes the result as
§9.14 fixes. A plugin that does not implement `relations()` yields
nothing and the collection has no relations member.

The error contract's exceptions are `Deferred` and `Unavailable`
(06 §6.1); `protocol_version` is `1`, and `relations()` is part of it:
the protocol is fixed with this version and not amended into it.

**Reference source types** (06 §6.5).

| type | fields |
|---|---|
| `RefSchema` | `{source_id, tables: [RefTable…], links: [RefLink…]}` |
| `RefTable` | `{name, columns: [{name, type: "TEXT" · "INTEGER" · "REAL"}…], primary_key}` |
| `RefLink` | `{name, from_table, from_column, to_table, to_column}` — declared by the referencing table |
| `RefRecordBatch` | `{table, rows: [object…]}` — rows keyed by the table's columns |
| `Entity` | `{entity_id: "<namespace>:<id>", kind: table name, source_id, label or null}` |

**Worked example.** PubMed's `origin.spec` as the Codex carries it:

```
{"checksums":{"algo":"md5","kind":"sidecar","suffix":".md5"},
 "delta":{"mode":"updatefiles-dir","subdir":"updatefiles"},
 "files":null,"filters":["baseline/*.xml.gz","updatefiles/*.xml.gz"],
 "layout":"container-files","listing":"directory","source_id":"pubmed",
 "transport":"ftp","url":"ftp://ftp.ncbi.nlm.nih.gov/pubmed/"}
```

The NLM's connection etiquette, the `demo` and `full` profiles and the
path of its terms file are in the plugin and not here; two peers that
create PubMed from the same plugin write this block byte for byte.

## 9.18 Format versions and unknown fields

Every artefact in this document carries `kind` and `version`; the pair
names one schema, the one its section fixes:

| kind | version | section |
|---|---|---|
| `louise-codex` | 1 | §9.3 |
| `louise-snapshot`, `louise-snapshot-sig` | 1 | §9.4 |
| record line, shard index (`LOUISE-PACK-IDX1`) | 1 | §9.5 |
| `louise-index` | 1 | §9.6 |
| bundle header | 1 | §9.7 |
| `louise-attestation` | 1 | §9.8 |
| `louise-digest`, `louise-event`, `louise-pull` | 1 | §9.9 |
| `louise-certifier`, `louise-pointer` | 1 | §9.11 |
| `louise-calibration` | 1 | §9.12 |
| `louise-calibration-run` | 1 | §9.12 |
| `louise-ack`, `louise-trust` | 1 | §9.13, §9.2 |
| `louise-seals` | 1 | §9.15 |

**Unknown versions.** A reader that meets a `version` it does not
implement refuses the object — exit 2 for a command, a dropped message
for a peer — and says which version it found; it never guesses, never
reads the fields it recognises and skips the rest. A Codex's `version`
is part of its bytes and therefore of its identity: a new Codex version
is written only by a new Codex.

**Unknown fields.** In a hashed or signed object an unknown key is
refused, both on write and on read: a field a later version adds — an
expiry, a revocation — must not be strippable by a peer running the old
version while the old signature still verifies. The opaque blocks of
§9.1 are the exception: their keys belong to the plugin or the verifier,
are hashed as given, and are never interpreted by a reader that does
not know them.

**Changing a format.** A change to any schema here is a new `version`,
never a silent extension. The platform image that introduces version
`n+1` reads version `n` for at least one release, writes `n+1`, and
`hold collection show` names the version of each artefact it holds. The
canonical form of §9.1, the two hash functions and the signature scheme
of §9.2 are not versioned by a field: they are what every version is
written in, and changing them is a new protocol, not a new version.
