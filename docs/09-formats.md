# 9. The SHARD format — every byte that is hashed, signed or moved

*(vocabulary amended 2026-10-08: SHARD, parts)*

A collection on disk is a **SHARD**: *Self-Hosted, Attested RAG
Distribution*, the format this document fixes. One directory holds one
collection — its Codex, its snapshots, its records, its vectors and
everything else a peer shares (02 §2.6) — and a peer that receives a
SHARD can check every byte of it against the identity it claims, which
is what the name is for.

**Bytes already written keep their names.** The format was called
*drag* (DRAG, *distributed RAG*) until 2026-10-08, and *pack* before
that; its pieces, now **parts**, were called *shards*. Renaming a key,
a magic or a path would change bytes that are hashed, signed or already
on disk, so none is renamed: the Codex's `slicing.shard_records`, the
snapshot's `shards`, the `shard` of a vector file's header, of a bundle
header, of a `bundle` event and of a coverage row, the attestation's
`shards`, the plan's `{"shards": …}` sample sizes, the rendezvous
`shard_id` and the magic `LOUISE-PACK-IDX1`/`LPK1` (§9.5) all keep the
old word. The `drag/` directory and `source = "drag"` (07 §7.8), which
no hash or signature covers, followed on 2026-10-11: `shard/` and
`source = "shard"`. Where this document writes *part*, a field
named `shard` means the same thing. A `credits` entry's `ids_per_shard`
and `shards` (§9.3) are another sense: they count a pre-embedded
archive's own shards, which are that archive's files, not parts.

This document is the byte-level reference the other eight point at. It
fixes, for every artefact whose identity is a hash, whose authorship is
a signature, or whose bytes cross a wire, exactly what those bytes are:
the path in the SHARD or the message on the wire, the fields and their
types, the rule by which the bytes are hashed or signed, and one worked
example on the project's own collections. Documents 02–08 say what an
artefact is *for* and point here for what it *is*; nothing in 02–08
restates a field table, and nothing here changes a rule stated there.
Where a section names a module of the platform, that module is the
implementation of the section, and the two agree byte for byte.

Two hash functions, two roles. `H` is SHA-256, and it is the function
behind every identity and every signed object: `collection_id`,
`space_id`, `lineage_id`, the snapshot hash, part roots and
`records_root`, bundle ids, attestation and event ids, the DHT keys and
the rendezvous scores. Its textual form is always `sha256:` followed by
64 lowercase hexadecimal digits. `content_hash` and `origin_hash` are
BLAKE2b with a 16-byte digest — 32 lowercase hexadecimal digits, no
prefix — and they are the only two places BLAKE2b appears: per-record
fingerprints inside parts, thirty-six million of them in PubMed, where
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
calibration member (§9.12) — the segment is the first 16 hexadecimal
digits of the digest after the prefix. A collection's own directory is
NOT one of these: it is the local name (07 §7.7, §7.8), which is a
peer's own word for the collection and not derived from its id.

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
`codex_hash`, a different value; its September cutoff snapshot is
announced under
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
the public key appears everywhere a producer or verifier is named. A
process that only recognises this peer's signatures reads the public key
alone, from the path `NODE_PUBLIC_KEY_PATH` names (07 §7.8): the textual
form above and a newline, nothing else.
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
| `quarantined` | list of `{key, attestation, time}` | yes | keys burned on this peer by a proof of forgery (03 §3.5): `attestation` is the id of the `forgery` attestation; the entry is removed only by `hold key trust <key> --restore`. The field keeps the name it had when a burned key was called quarantined; in prose the word now means a copy not yet tested (03 §3.6), and a key is *burned* |

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

**Path.** `codex.json` at the root of the SHARD (02 §2.6). Its bytes are
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
under `<LOUISE_ROOT>/collections/` is the LOCAL NAME, not the id
(07 §7.7's third form, and the tree of §7.8): a name holds one
collection for its life, and the collection's file names the id it
holds. A `codex.json` whose inner hash does not equal the id that file
names is refused; so is one pulled from a
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
| `version` | `6` · `7` · `8` · `9` | yes | the Codex format version (§9.18). Version 9 (*amended 2026-10-20*, docs/notes/gutenberg-graph-amendments.md, operator's decisions A-K1–A-K4) is this object with `slicing.container` (below) and, for an annotations Codex, `annotates` as a LIST of targets; it is written for a Codex that declares `container: "parquet"`, for an annotations Codex with more than one target, and for one whose claims use the `mention`, `relation` or `community` type — and for no other, so no Codex, space or `collection_id` already written moves (the two annotations collections of 2026-10-09 keep version 7 and their bytes). A version-9 annotations Codex writes `annotates` as a list even of one target, and a version-9 Codex carries `annotates` exactly when it is an annotations Codex (its `space` null); its `space.cut` is present where the token cut is declared and absent otherwise, since a Parquet collection need not cut by tokens. Version 8 is written for a space that declares the token cut (`cut`, below) alone (*amended 2026-10-18*, docs/notes/char-cut-truncation.md): it is the version-6 object with `space.cut`, its engines backend engines, and no other Codex is ever written at 8, so no Codex, space or `collection_id` already written moves; it is not an annotations Codex and carries no `annotates`. Version 7 is written for an ANNOTATIONS Codex alone (02 §2.10; *amended 2026-10-09*, docs/notes/annotation-collections.md): it is this object with `annotates`, `space` and `work` null and `layers` `[]`, and no other Codex is ever written at 7, so no Codex, space or `collection_id` already written moves. Version 5 is this object with no backend engine (below, *the engine*), and a Codex none of whose engines is a backend engine is written at 5, so no Codex, space, engine or `collection_id` already written moves and an argv engine or one RAGE stated keeps its bytes (*amended 2026-10-08*, docs/notes/vega.md); `space_from` copies a version-5 `space` into a version-5 Codex unchanged. Version 4 is this object with no layer carrying `input` — a layer of a version-4 Codex reads as `{"form": "text"}`, but writing the key into it would change its bytes and so its `collection_id` and `space_id`, so a version-4 Codex keeps both; `space_from` copying from a Codex below version 5 writes version 4, so that the copy's `space_id` is the source's (*amended 2026-10-08*, docs/notes/input-forms.md). Version 3 is this object with no engine carrying `options` — so a version-3 `space` is a valid version-4 one, and `space_from` copies it into a version-4 Codex unchanged (operator, 2026-10-03). Version 2 is this object with `space.encoder`, and a non-null `space.query.encoder`, as `{image, weights, label}` — the engine's image digest alone in place of the engine — that `image` inside `space_id`, and no `space.calibration.query_vectors`; version 1 is version 2 without `record_line`. A Codex already written stays at its version and cannot be moved, because `version` is inside the canonical bytes and so inside `collection_id` — changing it does not upgrade a collection, it names a different one. Version 2 is WRITTEN in one case only: a Codex whose `space` is copied with `space_from` (02 §2.7) from a version-2 Codex keeps that shape and that version, so its `space_id` is the source's — in the shape of version 3 and later the image leaves `space_id` and the copy would be another space, every vector of the source inadmissible to it. A version-2 space is never stated anew (operator, 2026-09-21) |
| `descriptive` | object | yes | `{title: string, summary: string}` — for people. The one key outside `collection_id`; covered by `codex_hash`; displayed, never used to sort, merge or resolve (08 §8.2). `title` is one line; `summary` is one short paragraph. Neither may be empty |
| `plugin` | string | yes | the plugin's `source_id` (06 §6.1); the uid prefix |
| `origin` | object | yes | the origin block, below |
| `toolchain` | object | yes | `{platform_image, mother_platform_image}` — image digests, `sha256:…`; `mother_platform_image` is `null` except on a daughter, where it is the mother's `platform_image` |
| `parser` | object | yes | `{id: string, version: integer}` — the readable alias of what the platform image contains |
| `cleaning_version` | integer | yes | the cleaner's version in the platform image (06 §6.3) |
| `protocol_version` | integer | yes | the plugin protocol version the plugin implements (06 §6.1) |
| `record_line` | `1` · `2` | yes | the shape of this collection's record lines (§9.5): `1` without a `figures` key, `2` with it. A collection never moves between them — one whose records gain plates is a NEW collection (02 §2.7) — and the format enforces it rather than a reviewer: this field is inside `collection_id`, so a Codex edited to change it is a different collection by construction. Carried by name and not folded into `version` above, because §9.18 versions the record line as its own schema: `version` tracks what wrote the Codex, `record_line` tracks what is in the parts, and a figureless collection created under a later format writes `version` 3 with `record_line` 1 |
| `layers` | list of layer | yes | `{chunker: {id, version, params}, rule, input}`, in evaluation order; the last rule is `"default"` (06 §6.3). `params` is an object whose keys the chunker id fixes, `{}` when it takes none. `input` is the encoder's input FORM for the layer's chunks — the model's documented input format, inside `space_id` with the rest of the layer (*amended 2026-10-08*, docs/notes/input-forms.md): `{"form": "text"}`, the chunk's text as it stands; or `{"form": "pair", "split": "first-section"}`, a tokenizer text pair — the record's first section's body and the rest of its sections' bodies joined by `"\n\n"` — admitted only on a chunker that makes one chunk per record (`abstract`). Any other form or split is refused. From version 5, required on every layer; below it, absent. `[]` on an annotations Codex (version 7), which cuts nothing |
| `slicing` | object | yes | below |
| `space` | object or null | yes | below; `null` exactly on an annotations Codex (version 7), which has no space: nothing pins it, it is in no ranked list and no vector is admissible to it (02 §2.10) |
| `work` | object | yes | `{tokens: integer, per: duration}` — the work parameter (02 §2.4): tokens of the encoder's tokenizer per `per`; `per` defaults to the update cadence when the recipe omits it, and is always written out; `null` exactly on an annotations Codex (version 7) |
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
| `origin_reachable_until` | snapshot id or null | yes | daughter: the last snapshot whose parts the origin still reproduces |
| `derived_from` | id or null | yes | sister and corrective sister: the mother's `collection_id` |
| `space_from` | id or null | yes | sister: the Codex whose `space` and `layers` were copied byte for byte. `cleaning_version` is not among them: a sister keeps her mother's in any case, since changing it would re-derive the corpus she inherits (02 §2.7) |
| `derivation` | object or null | yes | how this space's vectors may be obtained WITHOUT an encoder, when it is a pure function of another space's: `{from: id, kind: "truncate" · "quantise", params: {…}}`, `from` being a `space_id` and never a `collection_id`. `null` where there is none. NOT part of `space_id` (02 §2.4) |
| `replaces` | object or null | yes | corrective sister: `{codex: id, defect: {check, toolchain, witnesses, expected, observed, rationale}}` (03 §3.6); `witnesses` is a non-empty list of part names `"<slice>/<n>"` or uids |
| `facets` | list of objects | yes | which record fields a search may filter on and a reader may list (05 §5.7), sorted by `field`, `[]` when none. Each is `{field: string, kind: "flat" · "tree" · "range", separator: string or null, authority: {name: string} or null}`; `separator` is non-null exactly when `kind` is `"tree"`. `field` is a top-level record field (§9.5) or `extra.<key>`. Declares structure only: the values live in the records |
| `credits` | list of objects | yes | whom this collection's content and vectors are owed to, sorted by `(kind, archive, producer)`, never empty. Each entry has the same six keys — `{kind: "archive" · "community" · "corpus", archive: string or null, producer: string, license: string or null, attribution: string, coverage: "all" or {ids_per_shard: integer, shards: integer}}` — and `kind` fixes which of them carry meaning, below. Derived at creation from the plugin's declaration, the platform's registry and what is inherited, never composed by hand (02 §2.7); hashed, so no later build can alter or drop any of it |
| ↳ `kind: "corpus"` | | | the corpus provider — whom the TEXT is from. Exactly one entry, on every collection including green ones: every corpus has a provider and every provider is named, whether or not a licence compels it. `archive` is null, `coverage` is `"all"`, `license` is the corpus licence (`null` only where there is none to name), `producer` and `attribution` come from the plugin (06 §6.1) |
| ↳ `kind: "archive"` | | | a pre-embedded archive whose vectors this collection carries — zero or more, one per archive, `archive` naming it. Expanded from the platform's registry by the recipe's `archives`. `coverage` is load-bearing and not merely compliance: 06 §6.3's `archive:<id>` layer rule reads it to decide which layer a document falls in, and NO OTHER KIND is read there. The archive's SIGNING KEYS are deliberately NOT here: they are in the registry (06 §6.6), because a key is a changeable local fact — lost, rotated, or issued years after a collection was created (§9.2, "a lost key is a new key") — and freezing one into `collection_id` would fork the identity every time one moved, while blocking a collection from ever adopting an archive that started signing after it was written |
| ↳ `kind: "community"` | | | the peers who embed for this collection, as ONE collective and never as a roster. At most one entry, present exactly when `embedding` is `"open"` or `"endorsed"` and absent when it is `"certifier-only"` — so it is derived from a field already in the Codex rather than declared, and a collection that accepts no community vectors credits none. `archive` and `license` are null, `coverage` is `"all"` |
| `profile` | object or null | yes | plugin-defined descriptor, opaque and hashed (an ELI profile: `{eli: {jurisdiction, work_pattern, …}}`). One key the platform reads (*amended 2026-10-12*): `validity: {kind: "versions-per-work", from: "<record field>", work: "<record field>"}` declares a VERSIONED collection — every version of a Work is a record, `from` names the field holding the start of its validity (`extra.vigenza_from`), `work` the field naming the Work (`extra.eli_work`); a search then answers a date by validity and shows one version per Work by default (05 §5.1, 07 §7.3). The end of a version's validity is derived by the peer and is in no record |
| `annotates` | object (version 7) · list (version 9) | annotations Codex only | `{collection_id: id, from_snapshot: snapshot id}` — the TARGET collection an annotations collection makes claims about, and the target snapshot the claims were checked against and hold from (02 §2.10; *amended 2026-10-09*). At version 9 (*amended 2026-10-20*) a LIST of such objects in a fixed order, one per target — gutenberg-graph annotates gutenberg (the text) and wikidata (the entities); a claim's end names its target by the uid's plugin prefix, so no index is carried in the claim. Required and non-null at version 7; ABSENT, never null, below it — like an engine's `options`, so that every Codex already written keeps its bytes. An annotations Codex is also fixed in the rest: `plugin` `"annotations"`; `origin.spec` `{source_id: "annotations", nonce: <32 hex digits>}`, `unit` `"file"`, `cutoff_rule` `"record-date"`; `record_line` 1; `modality` `"text"`; `erasure` `append-only` or `retractable`; `privacy` `public` or `restricted`; `publication` and `embedding` `"open"`; `facets` `[]`; `credits` exactly one `corpus` entry, the annotator; every lineage field, `derivation`, `certifier`, `content` and `profile` null; and `validation.checks` drawn from `roots`, `records`, `license`, `signature`. Anything else is refused |

The relations are exclusive in this way: a daughter has `continues` and
`continues_through`; a sister has `derived_from` and, when it was made with
`--space-from`, `space_from`; a corrective sister has `derived_from` and
`replaces`; a root has none of them. Any other combination is refused.
`credits` descends by KIND, because the three kinds have three
lifetimes. The `corpus` and `community` entries are the same on every
relative without exception — a daughter, a sister in any space, a
corrective sister: the corpus is the same corpus, and the community will
embed for whichever of them accepts community vectors, so neither credit
is inherited work that a new space could leave behind. The `archive`
entries are: a daughter's equal her mother's, since she inherits the
vectors; a sister's equal her mother's when her `space_id` matches her
mother's and are her own recipe's when it does not, there being no third
case; a corrective sister has none, since it starts from zero (02 §2.7).
So `credits` is never `[]` — the corpus entry is always there — and the
part of it a relative may differ in is exactly the part that describes
vectors it did not compute.

**The origin block.**

| field | type | required | meaning |
|---|---|---|---|
| `spec` | object | yes | the identifying half of the acquisition spec, serialised as §9.17 fixes; a document whose text a credited archive supplies is located in that archive instead (06 §6.4), and the spec's shape does not change for it |
| `form` | string | yes | which origin form was taken and why, one sentence (06 §6.4) |
| `unit` | string | yes | how a record's origin bytes are reached, which is the cost of verifying one: `"file"` the origin publishes the document alone, `"container-member"` a container must be fetched to reach it, `"index-page"` an index makes the container seekable (06 §6.4). It never changes what `origin_hash` covers — that is always the one document (02 §2.3) |
| `clock` | string | yes | IANA zone of the origin's clock: `"America/New_York"`, `"UTC"`, `"Europe/Rome"` |
| `cutoff_rule` | `"dated-dumps"` · `"update-files"` · `"record-date"` | yes | how the origin's state at the cutoff instant is enumerated (06 §6.2) |
| `jurisdiction` | string or null | yes | ISO 3166-1 alpha-2, for a legal corpus; `null` otherwise |

**The slicing block.** `keys` is the ordered list of slice keys; each
key has a block; `shard_records` and `order` close the part rule
(`shard_records` keeps the old word: bytes already written keep their
names).

| field | type | required | meaning |
|---|---|---|---|
| `keys` | list of strings | yes | slice keys in path order: `["year"]`, `["lang", "pages"]`, `[]` for one slice `all` |
| `<key>` | object | one per key | for a year key: `{field, bins}` — `field` names the record field read (`date`), `bins` is the list below; for an id-range key: `{field, bin: integer}` — the record's `native_id` divided by `bin` (`field: "id"`: the number of the entity's own id, wikidata's `Q42` → 42); for a language key: `{field: "lang"}`; for a FAMILY key (*amended 2026-10-24*, pack-format.md §2): `{field: "extra.claims", rule: "wikidata-families", map: "sha256:…"}` — the family the rule gives the record's own statements under the map pinned at creation, whose bytes are the SHARD member `slicing/<sha256 hex>.json` (§9.4, §9.16; app/plugins/wikidata/families.py) |
| `unknown` | string | yes | the slice a record joins when the key cannot be read: `"year=unknown"` |
| `parts` | object | no | `{rule: "content", min: integer, max: integer}` — parts cut by content (02 §2.2, *amended 2026-10-24*): a record ends its part when the part holds `max` records, or at least `min` and the first 8 bytes of SHA-256 over the record's uid (UTF-8), big-endian, are divisible by `shard_records − min`; `1 ≤ min < shard_records < max`. Absent: parts cut every `shard_records`, closed parts kept where they ended |
| `shard_records` | integer | yes | records per part: 30,000 for PubMed, 2,000 for Wikipedia, 50 for Gutenberg; 1,000,000 for a claims collection in Parquet (below) |
| `container` | `"jsonl"` · `"parquet"` | version 9 | the record parts' file form (§9.5 "The file"): the zstd-framed JSONL of every collection so far, or one Parquet file per part. ABSENT, never null, below version 9, where a Codex reads as `"jsonl"`; REQUIRED at version 9, which is written for every collection kind that declares it (*amended 2026-10-20*, operator: Parquet parts offered to every collection, not to annotations collections alone). Inside `collection_id`: the container is part of what a part's bytes are, though not of what its records are — `records_root` does not depend on it |
| `order` | `"bytes"` · `"numeric"` | yes | how records sort within a slice: UTF-8 byte order of `native_id`, or integer order of it |

The `bins` grammar, evaluated in list order over a year `Y`: `"<N"` —
one bin for every year before `N`, named `pre<N>`; `"<k>y<N"` — bins of
`k` years from the previous bound up to `N`, named `<lo>-<hi>`; `"1y"`
— one bin per year from the previous bound on, named `<Y>`. A slice
name is `<key>=<value>` for each key in order, joined by `/`:
`year=1975-1979`, `year=2023`, `lang=de/pages=3M` (`pages` = `native_id
// bin`, written with `M` for a bin of one million). A slice name
matches `^[A-Za-z0-9=_\-][A-Za-z0-9=_.\-]*(/[A-Za-z0-9=_\-][A-Za-z0-9=_.\-]*)*$`;
it becomes a directory under `records/`. A segment of exactly 16
lowercase hexadecimal characters is refused, because it is the shape of
a `<space>` segment and would read as one in a member name (§9.16; until
2026-10-08 it collided `index-<slice>` with `index-<space>`).

**The space block.**

| field | type | required | meaning |
|---|---|---|---|
| `encoder` | object | yes | `{weights: "sha256:…", engine: <engine>, label: string}` — the weights digest (below: of the file, or of the directory where the model is one), the engine the reference vectors were computed with (below), and a readable label without guarantee. The engine's image is named here and nowhere else: `toolchain` pins the platform build only (02 §2.1), and §9.18 would refuse a Codex carrying a third `toolchain` key |
| `dim` | integer | yes | vector width as stored |
| `dtype` | `"f16"` · `"f32"` · `"i8"` · `"i5"` · `"i4"` | yes | storage type of vectors. The three integer types are quantised and carry a per-vector scale beside them (§9.6, §9.7); a space in one of them is normally reached by the top-level `derivation` above rather than by an encoder, because few encoders emit integers |
| `ctx` | integer | yes | the encoder's input window in tokens; a chunk is truncated to it. *Amended 2026-10-18* (docs/notes/char-cut-truncation.md): HOW is part of the function, so the space states it. A space that cuts by tokens has the encoder cut, with the model's own tokenizer, the platform sending the chunk whole with `ctx` as its window; every space before that day is cut by the platform to about `ctx` x 4 characters, which is how its calibration reference was made, and stays so. The field naming the cut is `cut`, below |
| `cut` | `"tokens"` | version 8 only | how an input longer than `ctx` is cut: by the ENCODER, at `ctx` tokens of the model's own tokenizer — VeGa's wrapper in front of a backend engine, the platform sending the input whole with `ctx` as its window (03 §3.3), since the platform holds no tokenizer. Inside `space_id`. ABSENT, never null, below version 8, and absent means the platform's cut to about `ctx` x 4 characters that every space written before 2026-10-18 was calibrated through (*amended 2026-10-18*, docs/notes/char-cut-truncation.md) |
| `pooling` | string | yes | `"cls"`, `"mean"`, `"last"` — the pooling the encoder is run with |
| `normalise` | boolean | yes | whether vectors are unit-normalised before storage; `true` in every text space |
| `query` | object | yes | `{encoder: {weights, engine} or null, instruction: string or null}` — an asymmetric space's query encoder, with its own weights digest and engine, and its prefix; both `null` in a symmetric space |
| `tolerance` | decimal | yes | the cosine a vector must reach against the reference (03 §3.1) |
| `calibration` | object | yes | `{set: "sha256:…", vectors: "sha256:…", query_vectors: "sha256:…" or null, quorum: {n: 16, min_pass: 12}}` — the calibration set's id, the reference vectors' hash and, in an asymmetric space, the query reference vectors' (§9.12), and the quorum |
| `envelope` | id | yes | the hash of the envelope tensor in the calibration member (§9.12) |
| `epoch` | string | yes | `"0"` for a root; a daughter AND a sister keep their mother's, so a sister of a corrective sister keeps the salted one and stays in her mother's space (02 §2.7); a corrective sister's is `H(canonical JSON of {"replaces": <replaced collection_id>})`, written `sha256:…`. Any other value is refused |
| `evaluation` | object or null | yes | `{suite: {name, version, sha256, public}, metric, value: decimal, conditions: {…}, coverage_check: {targets, resolved}, run_by: key, run_at: time, tier: "reproducible" · "asserted"}`; `null` when no score is claimed |

**The engine.** How the reference vectors were computed, written out
whole so that anyone can compute them again (02 §2.4):

| field | type | required | meaning |
|---|---|---|---|
| `name` | string | yes | the serving software: `"llama.cpp"`, `"text-embeddings-inference"`, the name of a server the platform ships |
| `build` | string | yes | its build or release as the software names it — `"b10630"`, `"1.8.0"`, a commit |
| `variant` | string or null | yes | the variant of that build where the software publishes several — `"sycl"`, `"cpu"`, `"cuda"`; `null` where there is one |
| `image` | id | yes | the digest of the OCI image holding that build, the one that was run; for a backend engine, the digest of the backend's installed files (below). It must be obtainable (02 §2.4), and it travels like weights (04 §4.3) |
| `args` | list of strings | yes | the argument vector the engine was started with inside `image`, program first, in order. The weights file is written `${WEIGHTS}`, the listening port `${PORT}` and, in a backend engine alone, the backend's directory `${BACKEND}` (below), and nothing else in it names the host |
| `env` | object | yes | the environment variables set for the engine beyond the image's own, name → string; `{}` when none. On a card it carries the vendor's bitwise-reproducibility mode (`GGML_SYCL_ENABLE_DNN=0` on Intel SYCL, `ROCBLAS_DEFAULT_ATOMICS_MODE=0` on AMD ROCm; CLAUDE.md invariant 12, docs/notes/approved-encoders.md): a card engine without it states no run. Never a credential: the Codex is public |
| `options` | object | no | the options the engine received when it loaded the weights, for an engine a model runner loads by a call rather than by its argument vector — a LocalAI backend's `LoadModel`. They are the runner's record of that call as the runner sent it (for LocalAI, `load_options` in `GET /system`, `deploy/localai/patches/`), with the weights file written `${WEIGHTS}`, the runner's models directory `${MODELS}`, and every number that is not an integer as its decimal string (§9.1). The variables the runner sets in the engine's environment — LocalAI's `EnvVars` — are the engine's `env` and are written there only, never again among the options (operator, 2026-10-03). An option naming any other absolute path makes the run unstatable: a run that loads a second file — a projector, a LoRA, a draft model — cannot be stated yet. ABSENT, never null, for an engine started by `args` alone, which therefore keeps the bytes and the `engine_id` it had in version 3 (operator, 2026-10-03). Never a credential |
| `device` | string | yes | the device the reference vectors were computed on, as its driver names it — `"Intel(R) Arc(TM) Pro B70 Graphics"`, `"cpu"` |

`name`, `build`, `variant` and `device` are readable; they are bound by
`image` and by the reference vectors, not by any check. `image`, `args`,
`env` and `options` are what a peer runs to reproduce the reference, and running
them on the named device is how a peer may get `space.calibration.vectors`
back bit for bit. **`engine_id`** = `H(canonical JSON of the engine
object)`. It names an engine wherever a producer's engine is recorded:
the bundle header (§9.7), attestations (§9.8), benchmarks (§9.9) and
calibration objects (§9.12).

**A backend engine** (*amended 2026-10-08*, docs/notes/vega.md). An
engine the model runner installed from its own gallery — a LocalAI
backend — and launches behind VeGa's wrapper (03 §3.3) is not in any
image the platform runs: the runner fetches it, upgrades it and moves
its tags as it likes (operator, 2026-10-08: no pinning). Its engine
object has the keys above, and the wrapper, which is the backend's
parent, writes them from what it launched, at every load:

| field | a backend engine's value |
|---|---|
| `name` | `"localai/<name>"`, `<name>` being the `name` the runner's install record (`metadata.json` in the backend's directory) gives, or the directory's name where there is no record |
| `build` | the `version` the install record declares; where it declares none — the case on the reference node — the first 12 hexadecimal digits of `image`'s tree digest, without its `sha256:` prefix. Never the record's `uri`, whose tag moves |
| `variant` | `null` |
| `image` | the **tree digest** of the backend's directory, below — never the digest the runner's gallery wrote into the install record, which is the registry's word for a tag at install time and not a digest of the bytes on disk |
| `args` | `["${BACKEND}/run.sh", "--addr", "127.0.0.1:${PORT}"]`: the argument vector the wrapper started the backend with, from the backend's directory, as the runner starts one; `${BACKEND}` is the backend's directory, whose bytes `image` names, so the vector is relative to it as the options are to `${MODELS}` |
| `env` | the variables the runner set for the load — LocalAI's `EnvVars` — as for any engine the runner loads by a call; what the backend's own `run.sh` exports is inside `image` |
| `options` | the `ModelOptions` of the `LoadModel` call exactly as the wrapper received them, read as protobuf JSON with the protocol's field names and unset fields omitted, then written as the `options` row states. A field the wrapper's copy of the runner's protocol definition cannot name refuses the load: an option missing from the engine would be a declaration where 02 §2.4 asks for what ran |
| `device` | the card the backend's processes hold memory on, as its driver names it, read from their DRM clients; `"cpu"` when they hold none |

The run's `driver` (§9.12) is the driver the backend loads, which may be
one it carries rather than the system's.

The tree digest, `image`, is

```
image = H( canonical JSON of { <path>: <entry>, … } )
```

over every entry below the backend's directory, the directory itself
excluded, walked without following symbolic links, except the install
record `metadata.json` at the top level — the runner's note of the
install, not a byte the backend runs, so a reinstall of the same bytes
is the same engine. `<path>` is relative to the directory,
`/`-separated, UTF-8. `<entry>` is, for a regular file,
`{"type": "file", "size": <bytes>, "sha256": "<64 lowercase hexadecimal
digits of SHA-256 over its contents>"}` (*amended 2026-10-13*, operator:
the execute bit is out — on the reference node's NFSv4-ACL dataset ZFS
synthesises it, and across a reboot the same untouched files reported it
on 700 instead of 5,734, moving the engine's identity; a bit that cannot
change a vector is not part of what computes one — a program without it
does not start, loudly); for a symbolic link,
`{"type": "link", "target": "<the link's own string>"}`, never followed;
for a directory, an empty one included, `{"type": "dir"}`. Anything else
— a socket, a FIFO, a device — or a name or link target that is not
UTF-8 states no run. Owners, times and modes are not in it.
It is written `sha256:<hex>` like every `H`. It is not the weights digest
of a directory: that one refuses links and maps a path to a digest, this
one keeps links, because a backend is a program and its links are part
of it.

A backend engine is told by its bytes: its program, `args[0]`, begins
with `${BACKEND}/`, which no other engine may carry, and its `image` is
then a tree digest and names no OCI image. It is admitted from Codex
version 6 and peer's calibration statement version 4 (§9.12, §9.18).

**A backend engine is archived.** The runner's gallery drops old
versions and moves its tags, and a verifier who wants the reference
vectors back needs the exact files the Codex names. So the node that
creates a Codex naming a backend engine keeps that backend's installed
directory for the Codex's life, as it keeps the platform image every
Codex names — on the reference node, beside those images on tank
(`louise-platform-images/`, deploy/reference/DEPLOYMENT.md). It
travels as an archive of the directory, whose own bytes are not the
identity: a peer that receives one unpacks it and computes the tree
digest, and that is the whole check. 04 §4.3's announcement and §9.16's
DHT key use the tree digest as they use an image's.

**The weights digest.** For a model held as one file — a GGUF —
`weights` is `H(bytes of the file)`. For a model held as a directory —
a Hugging Face snapshot, an OpenVINO IR beside its tokenizer — it is
`H` of the canonical JSON object that maps the path of every regular
file in the directory, relative to it and `/`-separated, to that file's
digest, `sha256:…`. A symbolic link is refused. Code the model loads
from its directory is a file like any other, because it is part of the
function: two copies that differ in it are two encoders. The same rule
gives the `weights` of a query encoder and of the run identity (§9.12).

**space_id.**

```
space_id = H( canonical JSON of
  {"cleaning_version": <int>,
   "encoder":          {"weights": "<space.encoder.weights>"},
   "layers":           <the layers list, byte for byte>,
   "space":            {"ctx", "dim", "dtype", "epoch", "normalise", "pooling", "query"
                        and, where the space declares it, "cut"}} )
```

`cut` enters exactly where the space declares it (version 8): a space that
declares none hashes as it always did (*amended 2026-10-18*).

`query` enters as the Codex writes it, except that a non-null
`query.encoder` is reduced to `{"weights"}`. `layers` carries each
layer's `input` from version 5, so the form the encoder is fed is inside
`space_id` (*amended 2026-10-08*).

Not `tolerance`, not `calibration`, not `envelope`, not `evaluation`,
not `work`: those are the contract around the function, not the
function. Not the `engine` and not the `label`: the engine is how the
reference was computed. Whether another engine computes the same
function is measured (03 §3.3), never declared (02 §2.4). `space_from` copies `space` and `layers` byte for byte and
never `cleaning_version`, which stays the mother's (07 §7.3); `derive`
refuses when the named Codex's `cleaning_version` differs from hers, and
refuses in any case when the resulting `space_id` differs from the
source's. The `<space>` path segment is the first 16
hexadecimal digits of the digest.

**The validation block.**

| field | type | required | meaning |
|---|---|---|---|
| `checks` | list of check names | yes | the checks that apply, from 03 §3.2's table of plan checks, in the order they run. §9.8's table is wider: it adds the results that are never planned — `merge`, `forgery`, `withdraw`, `supersedes` — which a peer produces from what it finds, not from a plan |
| `sample` | object | yes | per sampled check, its size: `{"records": {"shards": decimal}, "chunks": {"shards": decimal}, "records-vs-origin": {"shards": decimal}, "vectors-sample": {"shards": decimal}}` — a decimal is a fraction of the snapshot's parts (the key stays `shards`), `"1"` is exhaustive. The plan sets how many PARTS a check samples and never how many chunks: `vectors-sample` draws sixteen per sampled part and passes on the quorum `{n: 16, min_pass: 12}` of 03 §3.3, the same sixteen T3 draws by the importer's private randomness (03 §3.5). A plan-set count would need a scaling rule for the threshold that a creator could get backwards — raising the sample to strengthen the check and weakening it instead — and would drift from T3, whose randomness is deliberately unpublished |
| `required` | list of check names | yes | certified collections: what must have passed before the collection is shown as certified (03 §3.7); `[]` on an open collection |

`validation.json` at the SHARD's root is this block written out in
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
vitality: "actively-held" · "dormant", state: "refused" · "quarantine" ·
"in-use", reasons: [string…], origin_check: "passed" · "pending" ·
"acknowledged-unchecked" · "failed" · "not-planned"}`. `state` is the copy's state on
this peer (03 §3.6), derived when the command runs and stored nowhere;
`reasons` is empty exactly when `state` is `"in-use"`, and otherwise
names each test or acknowledgement the copy waits for, or why it is
refused (07 §7.8). `origin_check` is `"not-planned"` for a never-share
collection — a deposit collection (02 §2.9), whose origin is the client
that wrote it, so no `records-vs-origin` can run (operator, 2026-10-06).
Until 2026-09-19 this object carried `refused: string or null` in their
place. *Amended 2026-10-09*: it also carries `annotates`. For an annotations
collection (02 §2.10) it is `{collection_id, from_snapshot, target}`, where
`target` is the annotated collection's local name when this peer holds it
and null otherwise; for every other collection it is null. An annotations
collection's `space_id` is null.

**Worked example.** The PubMed collection's Codex, abbreviated to the
fields that carry its choices (every other field is present in the file):

```
{"kind":"louise-codex","version":4,
 "descriptive":{"title":"PubMed","summary":"Every MEDLINE abstract, sliced by year, for biomedical retrieval."},
 "plugin":"pubmed",
 "origin":{"spec":{…ftp.ncbi.nlm.nih.gov/pubmed/baseline and updatefiles…},
           "form":"the XML baseline and update files, because the API paginates and the FTP files are checksummed",
           "unit":"container-member","clock":"America/New_York","cutoff_rule":"update-files","jurisdiction":null},
 "toolchain":{"platform_image":"sha256:1f…","mother_platform_image":null},
 "parser":{"id":"pubmed","version":4},"cleaning_version":1,"protocol_version":2,"record_line":1,
 "layers":[{"chunker":{"id":"abstract","version":1,"params":{}},"rule":"default"}],
 "slicing":{"keys":["year"],"year":{"field":"date","bins":["<1950","5y<2000","1y"]},
            "unknown":"year=unknown","shard_records":30000,"order":"numeric"},
 "space":{"encoder":{"weights":"sha256:e2…","label":"MedCPT-Article-Encoder",
                     "engine":{"name":"llama.cpp","build":"b10630","variant":"sycl","image":"sha256:8c…",
                               "args":["llama-server","-m","${WEIGHTS}","--embedding","--pooling","cls","-c","512","-ngl","99","-np","1","--port","${PORT}"],
                               "env":{"GGML_SYCL_ENABLE_DNN":"0"},"device":"Intel(R) Arc(TM) Pro B70 Graphics"}},
          "dim":768,"dtype":"f16","ctx":512,"pooling":"cls","normalise":true,
          "query":{"encoder":{"weights":"sha256:b7…","engine":{…the same, its own weights…}},"instruction":null},
          "tolerance":"0.95","calibration":{"set":"sha256:6b…","vectors":"sha256:d4…","query_vectors":"sha256:5e…","quorum":{"n":16,"min_pass":12}},
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
`H` of the file exactly as written, `descriptive` included; the space
segment under `vectors/` is the first 16
digits of `H` over `{"cleaning_version":1,"encoder":{"weights":"sha256:e2…"},
"layers":[…],"space":{"ctx":512,
"dim":768,"dtype":"f16","epoch":"0","normalise":true,"pooling":"cls",
"query":{"encoder":{"weights":"sha256:b7…"},"instruction":null}}}` — the
engine nowhere in it.

## 9.4 The snapshot file and its sidecar

**Paths.** `snapshots/<snapshot_id>.json` — the snapshot, canonical
JSON, no signature inside; its hash is `H(bytes of the file)`.
`snapshots/<snapshot_id>.sig.json` — the sidecar of signatures, a set
that grows and is never hashed. Superseded record parts keep their old
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

*Amended 2026-10-23* (operator: annotations are updated, not replaced; 02
§2.10). An ANNOTATIONS collection's next snapshot may be written DURING
the period it names, not only after its cutoff: its origin is its
annotator, whose claims at the time of writing are the state, so no
cutoff passes over anything. Still one snapshot per period, consecutive in
the chain, and never ahead of the period open now.

**Fields.**

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-snapshot"` | yes | |
| `version` | `2` | yes | version 1 is this object without `figures`. A platform writing this document's format writes 2 for every collection, with `figures: []` where there are none; snapshots already written stay 1, and a chain links by `previous` hash rather than by version, so nothing is re-issued (§9.18) |
| `collection_id` | id | yes | the Codex this snapshot applies |
| `snapshot_id` | `YYYY-MM` or `YYYY-Www` | yes | by the cadence, below |
| `previous` | id or null | yes | the hash of the snapshot before; `null` on a root's first; on a daughter's first, the hash of the snapshot named by the Codex's `continues_through` |
| `shards` | list of part | yes | every record part the snapshot consists of, sorted by (`slice` bytes, `n`); the key keeps the old word |
| `relations` | list of relation part | yes | the relations parts (§9.14), same order; `[]` when the collection has none |
| `figures` | list of figures part | yes | the figures containers (§9.5), same order; `[]` when the collection carries no plates — which is every collection whose Codex has `record_line` 1 |
| `files` | object | yes | every whole-collection member with its transport root: `{"validation.json": {root, bytes}, "LICENSE-TERMS": {root, bytes} or null, "calibration/<set>.safetensors": {root, bytes}, "calibration/<set>.sig.json": {root, bytes}, "seals/<snapshot_id>.json": {root, bytes} or null}`, and `"slicing/<sha256 hex>.json": {root, bytes}` where the Codex's slicing block names a family map (§9.3; required then, absent otherwise — *amended 2026-10-24*). A SHARD file that is neither here nor a part that a `shards`, `relations` or `figures` entry names is not a member and is not served (07 §7.8). An annotations collection (02 §2.10) has no space, so its `files` carries no calibration entries: `validation.json`, `LICENSE-TERMS` and the seals file, the last two null when it is green and open (*amended 2026-10-09*) |
| `delta` | object | yes | `{added: [uid…], changed: [uid…], tombstoned: [{uid, reason}…]}`, each list sorted by uid bytes; `reason` is `"retracted"` (the origin says the record should not have existed) or `"withdrawn"` (the origin no longer supplies it) |

A part entry:

| field | type | required | meaning |
|---|---|---|---|
| `slice` | string | yes | the slice name (§9.3) |
| `n` | integer | yes | the part number within the slice, from 1; the file is `records/<slice>/<n as five digits>.jsonl.zst` — `.parquet` for a collection whose Codex declares `slicing.container: "parquet"` (§9.3, §9.5 "The file (B)"; *amended 2026-10-20*). The entry carries no container key: the Codex says it, once |
| `root` | id | yes | the BitTorrent v2 pieces root of the stored part file, `.jsonl.zst` or `.parquet` (§9.5) |
| `records_root` | id | yes | SHA-256 over the part's `content_hash` values as raw 16-byte digests concatenated in record order |
| `first_id`, `last_id` | string | yes | the `native_id` of the first and last record |
| `records` | integer | yes | records in the part |
| `chunks` | integer | yes | chunks over all its records under the Codex's layers |
| `chunks_root` | id | yes | SHA-256 over, in chunk order, each chunk's id as UTF-8 bytes followed by the raw 16-byte BLAKE2b-128 of its text. It depends on the records and the Codex's layers alone — exactly as the `chunks` count above already does — so "the snapshot is records only" (02 §2.2) still holds, and computing it costs the chunking pass the count costs anyway |
| `tokens` | integer | yes | `chunks × space.ctx` — the upper bound §4.5 and §4.7 use |
| `bytes` | integer | yes | size of the stored file |
| `toolchain` | `"own"` · `"mother"` | yes | which `platform_image` produced it: the Codex's own, or on a daughter the mother's for inherited parts |
| `writer` | string | no | the zstd that compressed the file, as `"pyarrow <version>"` (the library bundles zstd and does not export its version); a part whose root the previous snapshot listed keeps that entry's. A peer rebuilding the part from a patch (§9.9) matches the root for certain only under the same writer. Absent in snapshots written before 2026-10-12, whose bytes stay as they are (amended 2026-10-12, operator) |

A relations part entry has `slice`, `n`, `root`, `edges` (integer) and
`bytes`; it sits beside the record part of the same slice and number.

A figures part entry has `slice`, `n`, `root`, `figures` (integer) and
`bytes`, and sits beside the record part of the same slice and number,
holding every plate that part's records name (§9.5). Its `root` is here
rather than in a sharer's digest for two reasons: the container is
re-issued exactly when its part is, so it costs a snapshot nothing to
carry what it is already carrying; and a peer can price the plates —
often the larger half of a collection — before it fetches one byte. The
entry carries no digest over the figure hashes themselves: those are on
the record lines, whose part `root` this snapshot already fixes and a
certifier already signs, and a second digest over them would be a second
answer to a question that has one.

**Parts, exactly.** Within a slice, records sort by `native_id` under
`slicing.order`; they are cut every `shard_records` records; parts are
numbered from `00001` in that order. A later snapshot re-issues a
changed record's part under the same number with a new `root`; new
records fill the slice's open part — its highest-numbered, and the
only one that has not yet held `shard_records` records — re-issuing it
under its number, and open a new number only once it is full. Having
held that many closes it for good: dropping a tombstoned record at a
later re-issue leaves it short and it is never refilled, so "open" is
a part that has never been full, not one that is not full now; a
tombstoned record stays in
its closed part until that part is next re-issued and is excluded from
search and embedding by the tombstone alone. The set of parts re-issued
by a snapshot is the set whose `root` differs from the same `(slice, n)`
in `previous`; nothing lists it separately.

**Chain rules.** Consecutive snapshots have consecutive ids — the next
period of the collection's cadence, `2026-08` then `2026-09`,
`2026-W36` then `2026-W37`; each names
the hash of the one before; all name one `collection_id`, except a
daughter's first, whose `previous` belongs to the Codex named by
`continues` and whose inherited parts carry the mother's roots and
`toolchain: "mother"`. A peer applies a snapshot only when it holds the
one `previous` names.

**Re-running `hold snapshot create`.** For an id that exists: identical
bytes → nothing to do, exit 0; different bytes → refused, exit 2, the
first differing part named; while the fetch for that cutoff is
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
{"kind":"louise-snapshot","version":2,"collection_id":"sha256:9d3e…41ab","snapshot_id":"2026-08",
 "previous":"sha256:51c0…2e7a",
 "shards":[{"slice":"year=1975-1979","n":1,"root":"sha256:0be4…","records_root":"sha256:a91c…",
            "first_id":"1","last_id":"137402","records":30000,"chunks":30000,"tokens":15360000,
            "bytes":10485760,"toolchain":"self"}, …,
           {"slice":"year=2026","n":3,"root":"sha256:7d20…","records_root":"sha256:44f1…",
            "first_id":"40931122","last_id":"40952017","records":18317,"chunks":18317,"tokens":9378304,
            "bytes":6291456,"toolchain":"self"}],
 "relations":[],
 "figures":[],
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

## 9.5 Record parts

**Paths.** `records/<slice>/<NNNNN>.jsonl.zst` (`.parquet` under a Parquet Codex, §9.5 "The file (B)"; the superseded name below keeps the suffix) is the newest version of
part `n` of the slice. When a snapshot re-issues the part, the file it
supersedes is renamed `records/<slice>/<NNNNN>.<last snapshot id>.jsonl.zst`
— the last snapshot in which those bytes were current — and kept, so
that snapshot can still be served and checked (01, promise 7).

**The record line.** One record is one line: a JSON object with exactly
these keys, written in this order, compact (no whitespace outside
strings), UTF-8 with non-ASCII unescaped, terminated by `\n`. The
order is fixed so that two writers produce the same bytes; a line is
canonical JSON with the key order fixed by this table instead of by code
point.

**Two shapes, and which one a collection uses.** A line has no version
field of its own: its shape is its Codex's `record_line` (§9.3), which is
`1` for a line without `figures` and `2` for a line with it. The Codex
carries it because the shape is fixed for a collection's LIFE — a
collection whose records gain plates is a new collection (02 §2.7), never
the same one amended — and the Codex is the only immutable per-collection
object there is. A reader therefore knows which shape to expect before it
fetches a single part, `codex.json` being one of the three members
fetchable before an acknowledgement (04 §4.4).

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
| `figures` | list of figure | `record_line` 2 only | the plates this document carries, in `seq` order; `[]` when it has none, and `[]` on a pointer record. An entry NAMES a figure and never holds it — the bytes are the member `figures-<slice>` (§9.16), which a peer may hold or not. Absent entirely where `record_line` is 1 |
| `extra` | object | yes | plugin-defined, fixed keys; **everything the origin asserts about the entity that the keys above have no home for** (01 promise 9, 06 §6.4) — ids, publication details, classifications, keywords, an author's identifiers. Its keys are fixed for the collection's LIFE, so a field omitted here is not deferred: adding one bumps the parser and mints a new `collection_id` (§9.3, 02 §2.7). The deposit plugin's are 06 §6.7's: a collection answers at the parser version its Codex names, so one created before a key was added keeps the shape it had (amended 2026-10-25, operator: "Keep all", which added a source page's `html_*`). `origin_url` on a red-class source; `in_force` on a legal source; `seal` on a certified source whose origin file carries a seal, holding that file's `file_hash` (§9.15); `reason` never (a reason belongs to the tombstone) |
| `origin` | string | yes | the locator of this record's own origin bytes: their path relative to the origin base or their URL — a URL into a credited archive where that archive supplies the document's text (06 §6.4) — and where the document is a member of a container the path or URL followed by `#` and the plugin's member index (06 §6.4); never an absolute local path |
| `content_hash` | BLAKE2b-128 | yes | below |
| `origin_hash` | BLAKE2b-128 | yes | BLAKE2b-128 of this document's own origin bytes, the ones `origin` locates — one record, one hash, whatever the origin ships them in (02 §2.3); a record without one is refused at build |

**content_hash** = BLAKE2b-128 of the canonical JSON of the array
`[title, [[heading, text], …]]` — the title and the sections exactly as
the chunker sees them, so the same hash means the same chunks and the
same vectors whatever the origin's own revision fields say. A pointer
record (below) carries the `content_hash` of the text it points at.

**A FIGURE IS NEVER IN `content_hash`.** `content_hash` is over exactly
what the chunker reads, and a plate is not read by a chunker: its caption
is, and the caption is in `sections` like any other text. Putting a
figure in would make a record `changed` in the delta (§9.4) the moment a
plate arrived, refuse that record's rows out of every bundle already
computed for it (§9.7's `possession.json`), and re-embed text that never
moved. The two senses of "changed" stay apart: `delta.changed` means
RE-EMBED, a moved part `root` means RE-FETCH, and a plate arriving means
neither for the text.

**figures.** A figure is a plate, a diagram or a drawing the origin
carries beside the text. It is a MEMBER, not text. Each entry is an
object with exactly these keys in this order:

| key | type | meaning |
|---|---|---|
| `hash` | id | `H` over the figure's bytes as the origin ships them |
| `media_type` | string | the type the origin gives the figure, whatever it is (*amended 2026-10-25, "Every plate"*: until then one of 08 §8.7's admitted types); DISPLAY compares it against what the bytes actually are and never trusts it (08 §8.7) |
| `bytes` | integer | the size of those bytes |
| `seq` | integer | this appearance's position in the record's text, from 0 |
| `carried` | boolean | whether this collection carries the bytes. `true` for every image the origin has, whatever its type or size (*amended 2026-10-25, operator: "Every plate"*: 08 §8.7's closed list and caps bind display, not storage). `false` only where the origin names an image whose bytes do not exist to carry, and on records written before the amendment, which set it for a type outside 08 §8.7's list or a figure over a cap; such an entry is still written, because a reader that meets the caption and no picture must be able to tell "not here yet" from "not here ever". Kept in the format because records already published carry it |

`hash` is `H` — SHA-256 — and not the BLAKE2b-128 of `content_hash` and
`origin_hash` beside it, which is the one place in a record line the two
functions differ and is worth the sentence. Those two fingerprint text
this peer already holds and can re-derive; a figure's hash stands alone
over bytes that arrive SEPARATELY, from a container, and is the whole of
what a peer checks them against before storing them. A producer chooses
the hash it publishes, so the attack is a collision rather than a second
preimage — 2^64 work against BLAKE2b-128 — and the prize is a plate a
human will look at. §9.1's other reason for BLAKE2b does not apply
either: figures number in the hundreds of thousands, not the thirty-six
million of PubMed's records, so neither the cost of the function nor the
width of the field is the constraint.

**`figures` LISTS APPEARANCES, NOT DISTINCT PLATES.** A plate used twice
is two entries with the same `hash` and different `seq`; the container
stores its bytes once, being addressed by hash. The reason is
alignment: the record's text carries one `[Illustration…]` marker per
appearance (06 §6.4), so THE Nth MARKER IS `figures[N]` and a reader
returning part of a record can say which plates belong to the part it
returned (05 §5.3). Deduplicating would break that alignment from the
first repeat onward, silently and for most books — measured over
Gutenberg, 39 % of illustrated books repeat a plate, one of them 703
times over 66 distinct images.

The alignment is a rule about the WRITER: whatever produces the text and
whatever produces this list walk one filtered sequence of images, so a
cover and a broken reference drop from both together or from neither,
and every other image stays in both whatever its type.

**The figures container.** `figures/<slice>/<NNNNN>.fig.zst`, one
container per record part of the same slice and number, holding every
figure that part's records name with `carried: true`, each stored once
and ordered by `hash`. It holds OPAQUE BYTES: an SVG, a GIF or a plate
over 08 §8.7's caps is stored exactly as the origin ships it, verified by
its hash and nothing else, and never decoded here — whether it may be
shown is decided at display (08 §8.7, *amended 2026-10-25, operator:
"Every plate"*). A figure is addressed BY HASH INSIDE the container and
never by a path of its own, so §9.1's "path segments from digests" rule
does not reach here — no directory is ever named after a figure.

One container per part, and not one file per plate, because §9.16 makes
every FILE of a member its own BitTorrent v2 torrent. A corpus of
275,000 plates over 1,591 record parts is two orders of magnitude more
swarm state under one file each, for the same bytes and the same
integrity — the per-figure hashes on the record lines are what verify a
plate either way.

The container is re-issued exactly when its record part is, because a
record gaining or losing a plate rewrites its line and so moves the
part's own `root`. Its layout is that of the record part above — zstd
frames, then one skippable frame carrying the index — with the magic
`LOUISE-FIGURES-1` and, per entry, the 32-byte `hash` and the offset and
length of its bytes, entries ordered by `hash`.

**Chunks** are not stored: a record's chunks are recomputed from its
`sections` by its layer's chunker (06 §6.3), and a chunk id is
`<uid>:<seq>:<chunker version>`; the `chunks` check reproduces them and
the snapshot carries, per part, the count and the `chunks_root` (§9.4)
it reproduces against. Without a published root the check could compare
only a count, and a chunker that moved every boundary without changing
the number would pass — which 03 §3.4 leans on as half its proof that a
new platform image is equivalent.

**Claim records** (*amended 2026-10-09*; 02 §2.10). An annotations
collection's record is one CLAIM about records of the collection it
annotates, in the line above:

- `uid` is `annotations:<n>`. `native_id` `<n>` numbers the claims from 1
  in (`subject`, `object`, `type`) byte order, under `slicing.order`
  `numeric`, in the one slice `all` (`slicing` `{keys: [], unknown: "all",
  shard_records: 50000, order: "numeric"}`).
- `lang` is `"und"`. `title` is `""`. `creators`, `subjects` and `keys`
  are `[]`, `date` is null and `extra` is `{}`.
- `sections` is exactly one section, `["claim", <the claim's canonical JSON,
  §9.1>]`. The claim is there and not in `extra` so that `content_hash`,
  `records_root` and the `records` check cover all of it.
- `origin` is `claims.jsonl#<line>`: the claims file as the annotator
  submitted it, which the collection keeps beside its SHARD as its origin.
  `origin_hash` is BLAKE2b-128 of that line's bytes, without its line end.

The claim, an object with exactly these keys:

| key | type | meaning |
|---|---|---|
| `type` | `"same-work"` · `"translation-of"` · `"mention"` · `"relation"` · `"community"` | `same-work`: the two records are versions of one work in different languages, and neither is said to be the other's source; `translation-of`: the subject translates the object. *Amended 2026-10-20* (version 9; docs/notes/gutenberg-graph-amendments.md A-K1): `mention`: a span of a chunk of the text target names an entity of the entity target; `relation`: an entity stands in a relation to another, read from a chunk; `community`: a set of entities the method groups, with its summary |
| `subject`, `object` | uid, chunk or canonical id | for `same-work` and `translation-of`: two records of the annotated collection, different from each other. For `mention`: `subject` a CHUNK of the text target, `{uid, chunk_id, text_hash}` — `text_hash` the BLAKE2b-128 of the chunk's text as the target's chunker cut it (06 §6.3), the hash every stored chunk already carries — and `object` an entity as a canonical id `<namespace>:<id>` (`wikidata:Q42`). For `relation`: both ends entities as canonical ids. For `community`: `subject` `{id: <string the method mints>, level: integer}` and `object` null (A-K2) |
| `subject_lang`, `object_lang` | string | ISO 639 or `"und"`: the languages of the two versions. For `translation-of`, `object_lang` is the source language and `subject_lang` the target language. REQUIRED for `same-work` and `translation-of`; ABSENT for the three types of 2026-10-20 — an entity has no one language, and a chunk's is its record's |
| `start`, `end`, `surface` | integer, integer, string | `mention` only: code-point offsets into the chunk's text, `end` exclusive, and the text between them, so a reader shows the mention without the chunk |
| `relation`, `evidence_chunk` | canonical id, chunk | `relation` only: the property as a canonical id (`wikidata:P50`), and the chunk `{uid, chunk_id, text_hash}` the relation was read from |
| `members`, `summary` | list of canonical ids, string | `community` only: the members, sorted, no duplicates, at least two; and the method's text about them |
| `method` | object | `{kind: "matcher", id: string, version: integer}`, `{kind: "wikidata"}` or `{kind: "human"}` — how the claim was made. *Amended 2026-10-20*: `{kind: "model", id: string, version: integer, model: <sha256 of the weights>, prompt: <sha256 of the prompt text>, temperature: "0"}` for a model whose output is not bit-exact — what a verifier re-runs (03 §3.2 `model-agreement`) |
| `evidence` | object | what the method matched on; method-defined, no floats |
| `confidence` | decimal | its maker's confidence, `0` to `1` |
| `contributor` | string | who makes the claim |
| `subject_hash`, `object_hash` | BLAKE2b-128 | the two records' `content_hash` in `target_snapshot`, read from the parts that snapshot lists when the claim is written. A claim about a record that snapshot does not hold is refused. For a chunk end, the hash of the chunk's RECORD; the chunk's own `text_hash` is in the end itself. For a `community` BOTH are null — it has no record at either end; its members are checked held when it is written and not pinned |
| `target_snapshot` | snapshot id | the annotated collection's snapshot the claim was checked against; the first is the Codex's `annotates.from_snapshot`. With several targets (version 9) the snapshot of the target the SUBJECT belongs to; the object's target snapshot is the Codex's `annotates` entry for it |

One claim per pair and type. A `same-work` pair is the same claim either
way round. For the types of 2026-10-20 the claim is identified by its ends
and its own keys: (`type`, `subject`, `object`, `start`, `end`) for a
mention, (`type`, `subject`, `object`, `relation`) for a relation,
(`type`, `id`, `level`) for a community; at version 9 the claims are
numbered from 1 in the byte order of that identifying tuple's canonical
JSON (§9.1), and the two older types' tuple is (`subject`, `object`,
`type`) as before. Rules carried over: no floats anywhere (`confidence`
and any score a method states are decimal strings); strings NFC;
`edge_type` is `annotations:<type>`.

**Pointer records.** A red-class collection's records member is the same
line with `sections: []` and `extra.origin_url` set; every other field,
`content_hash` and `origin_hash` included, is that of the full record.
The text lives in the state directory of each peer that fetched it from
the origin (04 §4.4) and is never a member. The `records` check
recomputes `content_hash` only where the text is; on a pointer part it
checks the presence and form of the two hashes and nothing else.

**The file (A): zstd frames**, every collection so far, and every Codex
below version 9. The lines are concatenated in record order and compressed
as independent zstd frames of 1,000 records each (the last frame
shorter), zstd 1.5.x at level 9 as pinned by the platform image, no
dictionary, no checksum flag, content size written in each frame header.
After the last data frame comes one zstd skippable frame carrying the
seek index; standard zstd tooling ignores it, so `zstd -d
00001.jsonl.zst` prints the lines. All integers little-endian:

```
skippable frame = magic u32 (0x184D2A5E) ‖ size u32 ‖ payload
payload         = "LOUISE-PACK-IDX1" (16 bytes) ‖ raw_len u32 ‖ zstd(index JSON) ‖ frame_len u32 ‖ "LPK1"
```

The magic is `LOUISE-PACK-IDX1` and not the format's name: it was
written into every part built before the format was named, changing it
would make those parts unreadable for nothing, and no reader chooses
its behaviour from it.

`frame_len` is the length of the whole skippable frame (its 8-byte
header included), so a reader takes the last 8 bytes, seeks back
`frame_len`, and decodes. The index JSON, compact, keys in this order:

```
{"records": N, "chunks": C,
 "frames": [[offset, csize, rsize, n, first_uid], …],     -- absolute file offset, compressed and raw sizes, lines, first uid
 "uids": {uid: [frame, line], …}}                          -- in record order
```

**The file (B): Parquet** (*amended 2026-10-20*, operator's decision A-K4,
docs/notes/gutenberg-graph-amendments.md; a Codex whose
`slicing.container` is `"parquet"`, version 9, §9.3). At 10⁸–10⁹ claims a
JSONL part of 50,000 records is tens of thousands of parts, and the
snapshot's `shards` list alone is hundreds of megabytes every peer reads.
A Parquet part is `<NNNNN>.parquet` beside the record parts' path rule
above: one row per record in record order, one column per key of the
record line in the line's key order, every nested value (`creators`,
`subjects`, `keys`, `extra`, `sections`) as its canonical JSON text (§9.1)
in a string column, so that the LINE is a pure function of the row;
zstd-compressed columns; row groups of 1,000 records; the row group
metadata (its first row's `uid`) is the seek index, so there is no
skippable frame. `records_root` is computed exactly as for a JSONL part —
over the records' `content_hash` values (*Roots*, below) — so a Parquet
part and a JSONL part of the same records have the same `records_root`:
the record hashes never depended on the container. `root` is the pieces
root of the Parquet file's bytes, as of any stored file. The `records`
check reads either container; `hold records show` renders the §9.5 line
from a row. Offered to every collection kind, chosen per collection in its
Codex and fixed for its life.

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
{"uid":"gutenberg:1342","native_id":"1342","lang":"en","title":"Pride and Prejudice","creators":["Austen, Jane"],"date":"1998-06-01","subjects":["Courtship -- Fiction","England -- Fiction"],"keys":[],"sections":[["Chapter 1","It is a truth universally acknowledged, …"],["Chapter 2","Mr. Bennet was among the earliest …"],…],"extra":{"loc_class":"PR"},"origin":"1/3/4/1342/1342-0.txt","content_hash":"2c9e6f0b1a8d4e7f3b5c0a9d8e7f6a1b","origin_hash":"f1e2d3c4b5a69788796a5b4c3d2e1f00"}
```

A PubMed pointer record (red, text stays where it was fetched):

```
{"uid":"pubmed:38112034","native_id":"38112034","lang":"en","title":"","creators":["Rossi M","Bianchi L"],"date":"2023-12-19","subjects":["Humans","Neoplasms/genetics"],"keys":["pmid:38112034"],"sections":[],"extra":{"origin_url":"https://pubmed.ncbi.nlm.nih.gov/38112034/","revised":"2024-01-08"},"origin":"updatefiles/pubmed24n1220.xml.gz#5117","content_hash":"a3f0c1d2e3f4a5b6c7d8e9f0a1b2c3d4","origin_hash":"5e21f0a9b8c7d6e5f4a3b2c1d0e9f8a7"}
```

The pointer record's `title` is empty because a title is text; the full
record on a text-holding peer has the title and the abstract in
`sections`, and the same `content_hash`.

## 9.6 Vectors, centres and buckets

A peer's vectors rest in its own LanceDB table, one per space: assembled
from accepted bundles (§9.7), never a member's bytes, never fetched as
such — vectors travel only as bundles (04 §4.3). *Amended 2026-10-12*
(operator): a per-part file `vectors/<space>/<slice>/<NNNNN>.safetensors`
was specified here and never built; nothing reads or exchanges it, so it
is removed. The files below are under `vectors/<space>/`, `<space>` the
16-digit segment of §9.3; each is safetensors, tensor bytes
little-endian, row-major, header metadata values strings.

The row for a chunk is the vector from the first bundle accepted for it
(04 §4.5); a later bundle for the same chunk is compared and not
stored, unless the first is flagged. Labels are computed by the
importer against the centres it holds, never read from a bundle.

**The quantised types.** `i8`, `i5` and `i4` are symmetric absmax
quantisations of a unit vector, with a per-vector scale. A vector
tensor in one of them comes with a third tensor:

| tensor | type | required | meaning |
|---|---|---|---|
| `scales` | `[n]` f32 | for `i8`, `i5`, `i4` | row `r`'s scale; `0.0` for a row with no vector |

With `Q = 2^(bits-1) - 1` — 127, 15 and 7 — one vector `v` becomes

```
scale = max over d of |v[d]|
q[d]  = clamp(round_half_even(v[d] / scale * Q), -Q, +Q)
```

and is read back as `v'[d] = q[d] * scale / Q`. The negative extreme of
the two's-complement range (-128, -16, -8) is therefore NEVER WRITTEN,
and a reader that meets one refuses the file: the range is symmetric so
that negating a vector is exact, and a value outside it is a writer that
used a different rule.

Packing is little-endian by bit, element `d` occupying bits `[bd, bd+b)`
of the row's bit stream for `b` bits: `i8` is one byte per element,
two's complement; `i4` packs two elements per byte, element 0 in the LOW
nibble; `i5` packs eight elements per five bytes. A row occupies
`ceil(dim * b / 8)` bytes and any trailing bits are zero.

Quantisation is applied to the NORMALISED vector where `space.normalise`
is true, and the scale is then close to but not exactly `1/sqrt(dim)`'s
neighbourhood — it is stored rather than derived because a vector whose
largest component is unusual is exactly the vector that would suffer.

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
§9.5, and the header `chunk_ids_sha256` over them in row order (`vectors`
`[n, dim]` in `space.dtype`, `labels` `[n]` u32). Local
and optional; a request for a bucket group resolves to bundles (04
§4.3).

**The text index** is not a member (*amended 2026-10-08*; it was the cache
`index-<slice>`): each peer builds its own from the records it holds, and
nothing of it travels. The index is one per collection and language group,
not per part, so there was no per-slice unit to publish; the library has
no way to take in another peer's segments; and a peer rebuilds it at about
1,200 chunks a second, once per snapshot.

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
`collections/<name>/state/bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/`
— under `state/`, never in the SHARD: a bundle is neither named by the
Codex nor listed by a snapshot, so `share` would refuse a SHARD holding one
(07 §7.8), and §9.16 serves it from `state/bundles/`. Its four files take
their torrent paths relative to the bundle directory, which makes the
member root a function of the bundle alone; a bundle is a member
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
| `version` | `2` | yes | version 1 is this header with `encoder_image`, the engine's image digest alone, in place of `encoder_engine` |
| `collection` | id | yes | the collection the producer embedded for. Provenance, never a gate: a bundle is admissible in any collection whose `space_id` it matches (03 §3.5) |
| `space_id` | id | yes | the space, and the gate: a bundle whose `space_id` is not the importing collection's is refused at T0, before any tier runs |
| `snapshot_id` | `YYYY-MM` or `YYYY-Www` | yes | the snapshot whose records were embedded. Provenance, never a gate: an unchanged record keeps its vectors across snapshots (02 §2.2), and `possession.json` — not this field — decides whether a row still matches the text |
| `shard` | string | yes | `"<slice>/<n>"` — the record part the chunks come from; one part per bundle. The key keeps the old word |
| `uids` | list of strings | yes | the uids whose chunks the bundle covers, every chunk of each, sorted by bytes, no duplicates: the bundle's chunk range |
| `chunk_count` | integer | yes | rows in `vectors.parquet` after the challenge rows |
| `dim` | integer | yes | |
| `dtype` | `"f16"` · `"f32"` · `"i8"` · `"i5"` · `"i4"` | yes | the parquet element type; an integer type adds the `scale` column below |
| `producer_pubkey` | key | yes | the signer; `ed25519:…` |
| `encoder_engine`, `encoder_weights` | id | yes | the producer's engine (`engine_id`, §9.3) and weights; the Codex's, or a pair this producer's calibration statement admits (03 §3.3, §9.12) |
| `content_digest` | id | yes | the container-independent digest of the rows, below |
| `parquet_bytes`, `possession_bytes` | integer | yes | sizes of the two files |
| `created_at` | integer | yes | Unix seconds |
| `digest_alg` | `"sha256"` | yes | compared against the literal; never dispatched |
| `sig_alg` | `"ed25519"` | yes | likewise |
| `challenge` | object | yes | `{set: id, n: 16}` — the T2 challenge: the first `n` rows of `vectors.parquet` are the calibration set's first `n` texts (§9.12) embedded by the producer, with `chunk_id` `"calibration:<i>"`, `i` from 0 |

**vectors.parquet.** Two columns, `chunk_id` (string) and `vector`
(fixed-size list of `dim` elements of `dtype`) — and, where `dtype` is
an integer type, a third, `scale` (f32), carrying that row's absmax
scale (§9.6). The packing of a quantised row is §9.6's, byte for byte.
The challenge rows come first
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

written `sha256:…`. For an integer `dtype` a row's contribution is its
PACKED bytes as §9.6 lays them out — which are already byte-ordered by
that packing, so "big-endian" does not arise — followed by `u32be(4)`
and its `scale` as big-endian f32. The scale is inside the digest
because a vector and its scale are one value: two bundles agreeing on
every quantised integer and differing in one scale describe different
vectors, and a digest that missed it would call them the same.

**possession.json.** A JSON object, sorted keys, ASCII, mapping each uid
in `uids` to the `content_hash` of the record whose text the producer
embedded. The importer compares every entry with its own record. A uid whose
`content_hash` differs from this peer's copy has THAT RECORD'S ROWS
refused and the refusal recorded; the rest of the bundle is imported and
the import exits 0 (03 §3.5, 07 §7.3). A mismatch is the ordinary case of
a record re-issued since the producer embedded it, not a bad bundle —
vectors of another revision never attach to this text, and nothing else
in the bundle is implicated. A uid in `uids` with NO entry here is a
different thing: the bundle is malformed and is refused whole at T0.
Read only after the signature verifies; its digest is inside the signed
payload.

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

**Worked example.** A Gutenberg bundle, slice `lang=it`, part
`00007`, the first 300 chunks a contributor embedded before its first
partial publication:

```
header.json:
{"challenge":{"n":16,"set":"sha256:6b1a…"},"chunk_count":300,"collection":"sha256:e5c2…","content_digest":"sha256:0f7b…","created_at":1756713600,"digest_alg":"sha256","dim":1024,"dtype":"f16","encoder_engine":"sha256:5a…","encoder_weights":"sha256:e2…","parquet_bytes":662148,"possession_bytes":1417,"producer_pubkey":"ed25519:19…","shard":"lang=it/00007","sig_alg":"ed25519","snapshot_id":"2026-08","space_id":"sha256:41844ebb…","uids":["gutenberg:10102","gutenberg:1012","gutenberg:10125","gutenberg:10131",…],"version":2}
possession.json:
{"gutenberg:10102":"bb02…","gutenberg:1012":"7a1c…",…}
```

`uids` is sorted by bytes, which is why `gutenberg:1012` follows
`gutenberg:10102` — they agree through `gutenberg:101` and `0` precedes
`2` — while a uid that is a PREFIX of another precedes it, so
`gutenberg:1012` comes before `gutenberg:10125`. `possession.json`'s keys
are sorted the same way; the parquet's first sixteen rows are
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
| `version` | `3` | yes | version 2 is this body with `encoder_image`, the engine's image digest alone, in place of `encoder_engine`; version 1 is version 2 without the `figures` check |
| `collection_id` | id | yes | |
| `snapshot_id` | `YYYY-MM` or `YYYY-Www` | yes | |
| `snapshot_hash` | id | yes | which of two snapshots under one id was checked |
| `check` | string | yes | the check, below |
| `seed` | 64 hex or null | yes | the 32-byte seed of §9.1's sampler; `null` for an exhaustive or deterministic check |
| `shards` | list of strings | yes | the parts the check ran on, as `"<collection_id>/<space>/<slice>/<n>"` for a vector check and `"<collection_id>/<slice>/<n>"` for a record check, sorted; `[]` for a check without parts; the key keeps the old word |
| `result` | `"pass"` · `"fail"` · `"forgery"` · `"withdraw"` · `"deprecate"` · `"supersedes"` | yes | |
| `detail` | object | yes | per check, below; `{}` when the check has none |
| `platform_image` | id | yes | the image the verifier ran; when it differs from the Codex's and `check` is `records-vs-origin` with `result: "pass"`, the attestation is the equivalence proof of 03 §3.4 |
| `encoder_engine`, `encoder_weights` | id or null | yes | the engine (`engine_id`, §9.3) and weights used, for a statistical check and for a `forgery` proof (03 §3.5 has the proof carry the encoder: the recomputed hash means nothing without the encoder that made it; *amended 2026-10-07*); `null` otherwise |
| `key` | key | yes | the verifier |
| `time` | time | yes | |

**Checks and their detail.**

| check | kind | detail |
|---|---|---|
| `roots` | exact | `{failed: [part…]}` |
| `records` | exact | `{records: n, failed: [uid…]}` — content and origin hashes present and, where the text is, reproduced |
| `chunks` | exact | `{records: n, failed: [uid…]}` |
| `records-vs-origin` | exact | `{records: n, compared: m, origin_units: k, origin_units_sampled: K, origin_bytes: b, origin_bytes_bound: B, failed: [uid…], moved: [uid…], origin: "reachable"}` — `records` the sampled parts hold, `compared` those of the `k` origin units read out of the `K` the sampled parts reach into, `b` bytes read within the bound `B` (03 §3.2); a failing entry names the first differing record; `moved` names records whose line differs in `origin_hash` alone, which do not fail the check (03 §3.2, amended 2026-10-07; a reader of an older detail treats it as absent) |
| `image-equivalence` | exact | `{against: "reference-image", reference_image: id, records: n, compared: m, failed: [uid…], errored: k, reference_errored: j}` — `records` the reference re-parsed, `compared` those this image re-parsed, `failed` those whose line differs (03 §3.4, added 2026-10-07); never counted toward a trust level |
| `vectors-sample` | statistical | `{chunks: n, passed: n, median: decimal, failed: [chunk id…], producers: [key…]}` — the producers whose bundles supplied the sampled vectors; two producers when the attestation compares duplicate bundles (04 §4.5) |
| `calibration` | statistical | `{n: 16, passed: n, median: decimal}` |
| `centres` | exact | `{index_version: n}` |
| `labels` | exact | `{index_version: n, failed: [chunk id…]}` |
| `figures` | sampled | `{shards: n, named: n, held: n, failed: [hash…]}` — `named` counts the plates the sampled parts' records name and `held` those this peer has; `named - held` is not a failure, and `failed` names only plates whose bytes disagree with their hash |
| `license` | exact | `{}` |
| `signature` | exact | `{keys: [key…]}` — the sidecar signatures that verified |
| `anchor` | exact | `{state: "anchored" · "stale" · "mismatch"}` |
| `origin-seal` | exact | `{mode: "live" · "archival", entries: n, failed: [file_hash…]}` |
| `evaluation` | statistical | `{suite: {name, version, sha256}, metric, expected: decimal, observed: decimal, targets: n, resolved: n}` — the score `space.evaluation` claims, re-run on this copy; `resolved` is how many of the suite's targets this copy holds, and a run below the suite's own coverage floor is `result: "fail"` with `observed: null` |
| `merge` | exact | `{other: collection_id}` — two daughters' snapshots have one hash (04 §4.6); `snapshot_hash` is that hash |
| `forgery` | proof | `{chunk_id, producer: key, bundle: id, claimed: id, recomputed: id, text: string or null, content_hash: BLAKE2b-128}` — `claimed` and `recomputed` are `H` over the vector as big-endian `space.dtype` bytes; `text` is the chunk's text, or `null` on a red-class collection, whose `content_hash` stands in for it |
| `withdraw` | producer's own | `{bundles: [id…]}` — the signer retracts its own bundles; `result: "withdraw"` |
| `deprecate` | producer's own | `{bundles: {deprecated_id: current_id, …}}` — the signer marks copies of its own REDUNDANT beside an equivalent one it keeps (one `content_digest`, §9.7); `result: "deprecate"`. Unlike `withdraw` the copies are valid: peers keep serving them a day longer (07 §7.3) and list them under `deprecated_members` (§9.9), never start a fetch of one, finish a fetch in flight past half its bytes, fall back to one when the current copy is offered by nobody, and count two equivalent copies held together once (04 §4.5). Only the producer's own signature deprecates its bundle; the current copy may be another producer's (a peer that re-signed rows it landed defers to the original). *Added 2026-10-19, operator* |
| `supersedes` | claim | `{superseded: collection_id, evaluation: <the Codex's evaluation block>}` — a sister claims a better score; `result: "supersedes"` |

A failing statistical attestation lists the failing chunk ids in
`detail.failed`; a second key that re-embeds exactly those ids and fails
flags the collection (03 §3.6). Attestations older than two cadences do
not count toward a level.

**What `hold check --json` prints.** `{collection_id, snapshot_id,
snapshot_hash, checks: [{check, result: "pass" · "fail" ·
"inconclusive", detail, attestation: id or null}]}`; `attestation` is
the id of the attestation the run produced, `null` when inconclusive.

**Worked example.** A peer re-embeds sixteen chunks of one PubMed part
and finds a second producer's bundle compliant with the first:

```
{"body":{"check":"vectors-sample","collection_id":"sha256:9d3e…41ab",
         "detail":{"chunks":16,"failed":[],"median":"0.9987","passed":16,"producers":["ed25519:19…","ed25519:77…"]},
         "encoder_engine":"sha256:5a…","encoder_weights":"sha256:e2…","key":"ed25519:c3…","kind":"louise-attestation",
         "platform_image":"sha256:1f…","result":"pass","seed":"9b4e…d201",
         "shards":["sha256:9d3e…41ab/41844ebbc0d19f2e/year=2023/00011"],
         "snapshot_hash":"sha256:c07d…","snapshot_id":"2026-08","time":"2026-09-04T22:15:09Z","version":3},
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
| pull patch | `POST /api/share/patch/fetch` | `louise-pull` | `louise/event/v1` |

**The digest.** Sent hourly to every neighbour and to every address in
the peer list (04 §4.2).

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-digest"` | yes | |
| `version` | `2` | yes | version 1 is this object with `encoder_image`, the engine's image digest alone, in place of `encoder_engine` in `benchmarks` |
| `key` | key | yes | the sender |
| `time` | time | yes | |
| `address` | string | yes | where the sender answers pulls: `https://host:port`. A receiver whose `[peers.pins]` binds an address to the sender's key records the sender at THAT address and uses it for digests, events, pulls and swarm dials; the declared one is kept only as what the digest said (04 §4.2, amended 2026-10-19) |
| `neighbours` | list of `{key, address}` | yes | up to sixteen live neighbours |
| `benchmarks` | list | yes | `{space_id, encoder_engine, encoder_weights, tokens_per_s: integer}`, one per space, for the validated run this peer contributes with (03 §3.3, §9.12) — for the spaces of the collections it embeds under rendezvous and shares (`embed` p2p or all, `share` on; 04 §4.5, *amended 2026-10-20*): a peer that embeds nothing of a collection carries no row for its space and leads none of its parts. Keyed by space because that is how 04 §4.5 reads it: a peer with no entry for a collection's space is unscored for that collection. For a space whose collections are Codex version 2, whose one statement names the encoder's image and no run (§9.12, body version 1), the row carries `encoder_image` in place of `encoder_engine` — amended 2026-10-07, so that version-2 collections can be led under rendezvous at all; a receiver reads `space_id` and `tokens_per_s` either way |
| `collections` | list | yes | one entry per collection this peer SERVES a snapshot of — shared and with a snapshot built, below. A collection that cannot be taken from this peer is not announced: held but not shared, or created and never built, it is in no entry (*amended 2026-10-21, operator*: the node's wikipedia, a Codex and no records build, showed in every peer's Library as "still being built" with no take) |
| `peer_port` | integer | no | the port the sender's swarm listens on, as peers reach it (07 §7.8 `[node] peer_port`), so a fetcher with the DHT off reaches the members' swarms from the address alone (04 §4.3); absent when the sender names none. *Added 2026-10-06, migration step 9d; a version-2 digest without it is read as before* |

A collection entry:

| field | type | required | meaning |
|---|---|---|---|
| `collection_id`, `lineage_id` | id | yes | |
| `title` | string | yes | the `descriptive.title` of the Codex this peer holds, so a receiver can name a collection it has not fetched; untrusted, never used to sort, merge or resolve (08 §8.2) |
| `snapshot_id`, `snapshot_hash` | | yes | the latest snapshot held, and its hash |
| `codex_hash` | id | yes | `H` of the bytes of the `codex.json` THIS peer holds (§9.3). A `codex.json` pulled from this peer whose bytes hash otherwise is dropped: the pull path answers with stored bytes and no signature (below), so this is what makes it self-verifying. It is per-peer and not an identity — two honest peers may hold the same `collection_id` under different titles, hence different `codex_hash` (02 §2.1) — so it is compared only against the digest that advertised it |
| `license`, `privacy` | string | yes | the Codex's classes, so a receiver can skip what it may not take |
| `members` | list of `{name, root, bytes}` | yes | every member offered — what this peer SERVES, the narrowing of `share --slices/--kinds` applied, so a fetcher defers what no digest offers (04 §4.3, amended 2026-10-19) — by name (§9.16); a record part's root is the snapshot's, a bundle's root is its member root. A member no snapshot lists — a bundle, a text index — carries `files: [[path, root, bytes], …]` in path byte order as well, from which a fetcher computes each file's torrent (§9.16); the member root is `H` over that list. *Added 2026-10-07, migration step 9e: without it a bundle's four infohashes do not follow from the event or the entry* |
| `coverage` | list of `{shard, bundles: [id…]}` | yes | per part `"<collection_id>/<space>/<slice>/<n>"`, the bundle ids this peer has accepted through T0–T3, sorted — a deprecated copy held is among them (it is this peer's coverage of the part) |
| `deprecated_members` | list, as `members` | no | bundles their producer deprecated (§9.8 `deprecate`) that this peer still serves: listed apart so that nobody starts a fetch of one, present only when there are any — a reader that does not know the key sees the entry it always saw, and simply never falls back to one. *Added 2026-10-19, operator* |

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
| `bundle` | `{collection, space_id, snapshot_id, shard, uids: [first, last], bundle_id, root, bytes, files}` — `files` the four files as `[path, root, bytes]` in path order (§9.16), `root` the member root over them. *`files` added 2026-10-07, migration step 9e; a receiver leaves an event without it alone* |

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
| `what` | `"codex"` · `"snapshot"` · `"attestations"` · `"patch"` | yes | |
| `collection_id` | id | yes | |
| `snapshot_id` | `YYYY-MM` or `YYYY-Www`, or null | yes | for `snapshot`; `null` means the latest the responder holds |
| `since` | time or null | yes | for `attestations`: only those with `time` after it |
| `part` | string | for `patch` | the record part, `<slice>/<NNNNN>` |
| `have` | list of hex | for `patch` | SHA-256 of each line (without its newline) of the version the asker holds — for a plates container, the plate hashes it holds — at most 200,000 |
| `part_kind` | `"figures"` | no | the part's plates container (§9.5) instead of its records; absent for a record part |

The response is one JSON object `{"items": [{"path": string, "sha256":
id, "b64": string}…]}` — each item the exact stored bytes, base64 —
never a re-serialisation: `codex.json` for the collection and for every
Codex in its chain (`path` is `codex/<collection_id hex>.json`);
`snapshots/<snapshot_id>.json` and `snapshots/<snapshot_id>.sig.json`;
attestation
envelopes as `attestations/<id hex>.json`. The receiver hashes each
item's bytes, compares with `sha256` and with what a digest advertised,
and treats a mismatch as 04 §4.2 says.

**The patch** (amended 2026-10-12, operator; docs/notes/part-deltas.md).
A `patch` pull asks a peer holding a part as `snapshot_id` lists it for the
lines the asker's version lacks. The answer is one item,
`patch/<slice>/<NNNNN>.json.gz`: gzip (no time) of canonical JSON
`{"version": 1, "records": N, "chunks": C, "frames": [lines per frame…],
"order": [ …a line hash the asker sent, or an index into "new"… ],
"new": [ …line text… ]}`, or no item when the responder does not hold that
version. The asker rebuilds the part with §9.5's writer — the lines in
`order`, cut into frames of `frames`, `chunks` in the index — and promotes
it only when it hashes to the root the snapshot lists; a rebuild that
misses is set aside and the part is fetched whole (04 §4.3). A miss is
never by itself a lie: a writer built against another zstd gives other
bytes, so a part entry names its `writer` (§9.4) and a peer under another
does not ask. A responder keeps each patch for `[share] patch_cache_s`
(07 §7.8), keyed by the new root and the asker's set of lines.

A plates container (`part_kind: "figures"`; amended 2026-10-12, operator)
is answered as `patch/figures/<slice>/<NNNNN>.patch`: one line of canonical
JSON `{"version": 1, "kind": "figures", "plates": [[hash hex, length,
frame bytes]…]}` in hash order, `frame bytes` 0 for a plate the asker
named, then those frames, verbatim and in that order. The asker lays the
container out again from its own frames and these (§9.5's layout); nothing
is recompressed, so no writer condition applies.

**Worked example.** A digest entry for the Gutenberg collection held by
a peer that serves Italian and English and has accepted one bundle on
part 7:

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
| `shard` | string | `"<collection_id>/<space>/<slice>/<n>"` — the part, by its rendezvous `shard_id` (04 §4.5) |
| `bundle_id` | id | |
| `producer` | key | |
| `uids` | `[first, last]` | the bundle's chunk range |
| `chunks` | integer | rows merged from it |
| `tiers` | object | `{T0, T1, T2, T3}` each `"pass"` · `"fail"` · `"skipped"`; T1 is skipped on a non-`fixed` layer, T1, T3 on a peer without the text |
| `accepted_at` | time | when it passed this peer's ladder; `null` while it has not |
| `source` | `"local"` · `"fetched"` · `"archive"` | who produced the vectors from this peer's view |
| `verified_on_use` | integer | chunks of it T4 has re-embedded and confirmed |

Derived from the rows, per part: `covered` — the chunks the accepted
bundles' chunk ids cover TOGETHER, the union read from the held
`vectors.parquet` files where a part has more than one accepted bundle
and the row's `chunks` where it has one (04 §4.5, amended 2026-10-19: a
sum closed a part over a gap); `open` — the part has any chunk without one; `leader` — the
first peer of the part's rendezvous ordering, information and not a right (04 §4.5, *amended
2026-10-20*: the open parts are a bounty list any peer may embed; `window_ends` is null
since no grace window gates embedding). A coverage claim in a neighbour's digest is
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

**Worked example.** PubMed, `year=2023`, part 11, on a peer that
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
| `seq` | integer | yes | strictly increasing per collection; a peer keeps the highest verified `seq` and discards lower ones. The BEP 44 item's own `seq` is the DHT's: the libtorrent binding manages it (*amended 2026-10-07*: `dht_put_mutable_item` takes none and increments the stored one), so a peer judges by this field, which the certifier signed, and the DHT's only keeps the newest put winning |
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
texts shipped with the platform, `calibration/<set>.jsonl` in
the platform image — one line per text, `{"i": <0…511>, "text": "…"}`,
compact, `\n`-terminated. Its id is `H(bytes of the file)`; `<set>` in
a path is the id's 16-digit segment. The platform ships one set per
modality; the Codex names the one it uses in `space.calibration.set`.

**A pair space's set** (*amended 2026-10-08*, docs/notes/input-forms.md).
A space whose layers feed the encoder text pairs (§9.3's `pair` form) is
calibrated in that form, because a gate over plain texts would pass a
backend that flattens pairs — the defect the form exists to keep out. Its
set is 512 title-and-abstract PAIRS drawn from the collection's own
records: a file of lines `{"i": <0…511>, "pair": ["<first>", "<second>"]}`,
compact, `\n`-terminated, each pair's two segments non-empty, NFC, and
free of control characters but the line feed that joins an abstract's
sections. Its id is `H(bytes of the file)`, named by
`space.calibration.set` like any set's. The draw is fixed by a snapshot
of the collection alone: its parts `sample(<the snapshot hash's 32
bytes>, 16, <the part names "<slice>/<NNNNN>">)` (§9.1), walked in
snapshot order; each of
their records cut in the pair form of its layer — the first section's body
and the rest joined by `"\n\n"`, as §9.3 states it — the first of any
identical pairs kept; the candidates ordered by the UTF-8 byte length of
the two segments together, ties by the uid's bytes; and those at ranks
⌊k × (N − 1) / 511⌋ for k = 0 … 511 taken, in that order — the shortest,
the longest and the rest spaced between (`tools/build_pair_calibration_set.py`).
It is not shipped with the platform, since those records are the
collection's and under its licence: it travels inside the member, as
`pairs` below, so it is fetched after the acknowledgement like every
member but the three of 04 §4.4. Every use of the set embeds a pair as a
pair on the document side; the query side, whose input is one text in
every space, embeds each pair as `<first>\n\n<second>`, with
`space.query.instruction` prefixed. A space whose layers feed both forms
has no one set and is refused.

**A set spans the encoder's whole input range, and that is a
requirement.** It read "512 short texts" until 2026-09-09, and short
texts alone cannot see the failure this gate exists to catch: a build
correct on a sentence and wrong on a full context window passes every
check while the corpus takes damage — which is not hypothetical, it is
what happened here, and the trial that missed it used sixteen short
texts. So a set carries texts at every length up to the largest `ctx`
any space it serves declares. A text longer than a given `ctx` is
truncated deterministically, so ONE long text is a full-window probe for
a small-`ctx` encoder and a genuinely long input for a large one, and a
single set therefore serves every space: it needs to reach the largest
window, and the smaller ones come free. It keeps the short end too —
degenerate inputs, tokeniser edges, near-duplicate pairs differing in
one controlled way, and every script the corpora use — because those are
the regimes a hash cannot check and length is not the only one.

None of this is hashed: the set's id is its bytes, so a set that does
none of it is still a valid set, and the requirement binds whoever
composes one rather than whoever reads it. What a peer checks is that
the id matches; what this paragraph fixes is what a set is FOR.

**The member.** `calibration/<set>.safetensors` at the SHARD's root,
written by `hold collection create` (06 §6.6) and a member of every
snapshot (`files`, §9.4):

| tensor / header | type | required | meaning |
|---|---|---|---|
| `vectors` | `[512, dim]` f32 | yes | the reference vectors, in set order, as the creator's encoder produced them, unit-normalised when `space.normalise` |
| `query_vectors` | `[512, dim]` f32 | when `space.query.encoder` is not null | the query encoder's reference vectors for the same texts, `space.query.instruction` prefixed — a pair set's as `<first>\n\n<second>` (*amended 2026-10-08*): what an asymmetric space's query runs are validated against (03 §3.3) |
| `envelope` | `[2, dim]` f32 | yes | row 0 the per-dimension mean of `vectors`, row 1 the per-dimension standard deviation |
| `pairs` | `[bytes]` u8 | exactly when the space's layers feed pairs | the pair set, the bytes of its file (above) (*amended 2026-10-08*) |
| `set` | header | yes | the set's id |
| `encoder_engine`, `encoder_weights` | header | yes | the run the vectors came from: `engine_id` of `space.encoder.engine` (§9.3), and the weights |
| `key` | header | yes | the creator's key |

`space.calibration.vectors` = `H` over the bytes of the `vectors`
tensor; `space.calibration.query_vectors` = `H` over the bytes of the
`query_vectors` tensor, `null` in a symmetric space; `space.envelope` =
`H` over the bytes of the `envelope` tensor; `space.calibration.set` =
`H` over the bytes of the `pairs` tensor, in a pair space; a member whose
tensors hash otherwise is refused. Beside it,
`calibration/<set>.sig.json` is an envelope signed under
`louise/calibration/v1` with body `{kind: "louise-calibration", version:
2, set, space_id, vectors: id, envelope: id, encoder_engine,
encoder_weights, key, time}` — who produced the reference, for
provenance; the Codex's hashes, not the signature, are what bind it.
Version 1 of that body carried `encoder_image`, the engine's image
digest alone, in place of `encoder_engine`. Version 3 is the same body
signing a member that carries `pairs`, and is written for such a member
alone: a text space's member and body are what they were, at version 2,
so no text space's calibration moves (*amended 2026-10-08*).

**Three uses, one file.** `hold embedder check` embeds the 512 texts,
draws `quorum.n` of them by `sample(seed, n, {"0"…"511"})` with the
peer's own seed, and passes when at least `quorum.min_pass` reach cosine
≥ `tolerance` against `vectors` and the median does; the same run
measures the benchmark in tokens per second (03 §3.3). T1's probe set is
`vectors`; T0's envelope is `envelope`; T2's golden pairs are texts
0–15 with their rows (§9.7) — in a pair space items 0–15, embedded as
pairs (*amended 2026-10-08*). Two sisters on one encoder share one
calibration member byte for byte (08 §8.3). Cosines are computed in f32
after upcasting.

**The run identity** (03 §3.3). What the platform states for every
answer of every engine the node's model runner launched:

```
run_id = H( canonical JSON of
  {"driver":  "<the device's kernel driver and compute runtime, with versions, as the platform reads them; \"cpu\" on a CPU>",
   "engine":  <the engine object of §9.3, as LAUNCHED: `image` the digest of the image actually running, or of a backend's installed files>,
   "weights": "<the weights digest of §9.3, of the file or the directory actually loaded>"} )
```

The platform computes it from what it launched, never from a
declaration, and states it in one of two ways (*amended 2026-10-08*,
docs/notes/vega.md): VeGa by a **receipt** for every input (below);
RAGE, until it retires, in the response header `Louise-Run` of every
answer, which stays defined as it was for the endpoints behind RAGE and
counts for nothing on an answer through VeGa. Each describes a run on
request — VeGa at `GET /vega/runs/<run_id>`, RAGE at
`GET /louise/runs/<run_id>` — answering with that same object, which is
where a statement's `engine`, `weights` and `driver` come from.

Five things make "what it launched" computable, and a platform that
states a run follows all five:
- **`image`.** Behind RAGE: a process cannot see the digest of the image
  it runs in; only the container runtime knows it. RAGE therefore learns
  its `image` from a deployment pinned by digest and refuses to start
  otherwise: a tag is not an image. A digest-pinned deployment IS the
  observation, since the runtime runs nothing else. An engine the
  runner installed after the image was built — a backend fetched at
  run time — is not in that image, and RAGE states no run for it.
  Behind VeGa every engine is such a backend, and its `image` is the
  tree digest of its installed files (§9.3), which the wrapper computes
  at every load while the backend loads, reading again only a file whose
  device, inode, size, modification or change time at its physical path
  moved since it was last read.
- **`args` and `env`.** They are the engine's as §9.3 writes them —
  `${WEIGHTS}`, `${PORT}` and `${BACKEND}` unsubstituted, and only the
  variables set for the engine — never the process's raw argument vector
  and environment, which carry a port, a path and a container's host
  name that change at every start and would make every restart another
  run. RAGE checks that the process serving the answer is the one it
  read them for — by pid and start time, before and after the answer —
  and that it matches them; a process that does not match states none.
  VeGa's wrapper is the backend's parent: the vector is the one it
  passed and the variables the ones it gave, and a load whose variables
  are not in the environment the backend was started with states no run; an
  answer given while the backend was reloaded or had exited gets no
  receipt.
- **`options`.** For an engine that receives its load options over its
  own protocol, the options as the runner sent them (§9.3) — behind
  RAGE read at every launch from the runner's record of that load, never
  from its configuration, which the runner completes with defaults of
  its own; behind VeGa the load call itself, which the wrapper receives.
  The environment the runner sets for it is its `env`, not an option.
  Absent for an engine started by its argument vector alone.
- **`weights`.** Hashed at every launch, from the bytes on disk; behind
  VeGa while the backend reads them, and a load whose weights are not
  hashed within half an hour states no run.
- **`driver`.** The kernel's release and the kernel module the card is
  bound to, with its version, as the platform reads them, then the
  compute runtime. Behind VeGa the runtime is the driver library the
  backend loads (*amended 2026-10-08*): the one its processes' own
  environment directs the compute runtime to (on the reference node
  Level Zero's `ZE_ENABLE_ALT_DRIVERS`, which LocalAI's `run.sh` exports;
  the environment the wrapper gave, where none can be read), else the
  one its own `lib/` carries; each written `<file name> sha256:<hex>`,
  the file a link names, so its version is in the name and its bytes in
  the digest. Processes naming two drivers, or a `lib/` carrying two,
  state no run. A backend that loads no driver of its own is read as
  before, from the runner's image's package database. Never the runner
  image's driver for a backend that loads its own: that is a driver the
  backend does not run.

So the same engine relaunched unchanged states the same run, and a
changed flag, a changed load option, a changed weights file, a changed
image or backend file or a changed driver each states another.

**The tag, the receipt and the receipts route** (*amended 2026-10-08*,
docs/notes/vega.md; operator's proposal, approved in principle, its
bytes under review). How VeGa states the run of each answer, and how
Louise reads it. The tag, the two digests and the receipts route are a
**wire format**: what Louise sends through the runner, what the wrapper
digests, and what VeGa answers. The intake between the wrapper and VeGa
is not: both ends are this platform on one node, and it is fixed here
so that two programs agree. The runner's own API — LocalAI's
`/v1/embeddings` — is the runner's, and Louise fixes only the two things
below in it.

*The request.* Louise sends an embedding request to the runner, naming
the encoder's `vega-` model, with every input TAGGED and
`"encoding_format": "base64"`, so that the vectors come back as the
float32 bytes the backend produced and no decimal is read at all. A
text-pair input is sent as a tagged pair, never in another field.

*The tag.* Every input is one string:

```
"\x1eVEGA/1\x1e" + canonical JSON (§9.1) of
      {"form": "text", "id": "<id>", "text": "<the text>"}
   or {"form": "pair", "id": "<id>", "pair": ["<first>", "<second>"]}
```

The marker is the eight characters U+001E, `VEGA/1`, U+001E — bytes
`1e 56 45 47 41 2f 31 1e` — which canonical JSON never contains raw. `id`
is 128 random bits as 32 lowercase hexadecimal digits, fresh for every
input and never reused. A pair is the layer's `pair` form (§9.3). The
wrapper refuses, and answers nothing for, an input that does not start
with the marker or carries it elsewhere, a body that is not exactly one
of the two objects — another key, a missing one, an id of another form,
a number, a duplicate key, any byte the canonical form would write
otherwise — a text carrying the pair marker of the backend's own pair
form, which only the wrapper writes, a pair sent to a backend loaded
without `pairs:true`, and a request that carries token ids, which no
tag can bind. It strips the tag and forwards the bare text, or the pair
in the form the backend reads. `VEGA/1` is the version of this protocol
— the tag, the receipt and the routes together — and a change to any of
them is `VEGA/2`.

*The two digests.* The **input digest** is `H` of the canonical JSON of
the tag's object without its `id` — `{"form": "text", "text": "…"}` or
`{"form": "pair", "pair": ["…", "…"]}` — what was asked, independent of
who asked. The **vector digest** is `H` over the vector as IEEE-754
binary32, little-endian, its components concatenated in order: the bytes
the backend produced. Both are written `sha256:<hex>`.

*The receipt.* For every input it answers, the wrapper sends VeGa

```
{"id": "<the tag's id>", "run_id": "sha256:…", "input_digest": "sha256:…", "vector_digest": "sha256:…"}
```

and returns the vector only after VeGa acknowledges holding it. With no
acknowledgement within two seconds it fails the call with the gRPC status
UNAVAILABLE and a message containing `VeGa unavailable`, which the runner
passes on and Louise reads as a deferral: never a vector without its
receipt. An answer the runner would transform before returning it —
per-token vectors it pools itself — gets no receipt: the wrapper refuses
it, since no receipt could name the vector Louise would receive. The
receipt is not signed and has no id of its own: it is read once, from
this node's VeGa, by this node's writer.

*The routes*, read-only HTTP at `VEGA_URL` (07 §7.8):

| route | answer |
|---|---|
| `GET /vega/receipts?ids=<id>,<id>,…` | `200 {"receipts": [<receipt>, …]}` — one per id VeGa holds; an id it does not hold is simply absent. 1 to 1,024 ids, comma-separated; any other request, or an id of another form, is `400` |
| `GET /vega/runs/<run_id>` | `200` the run object above, `{"driver", "engine", "weights"}`; `404` for a run VeGa was never told |

VeGa keeps a receipt a bounded time — ten minutes, at most 100,000 —
since it is read seconds after its answer; one gone when Louise asks is
a deferral, and she embeds again. It keeps runs across restarts, since a
statement may be written long after the answer that named its run. An
id is one-time: the first receipt for it stands, the same receipt again
is acknowledged, and another one for that id is refused.

*The intake* (platform-internal). A unix socket on a dataset the
runner's container and VeGa's both mount; one canonical-JSON line each
way. The wrapper sends `{"kind": "run", "run_id", "run"}`, accepted only
when `run_id` is `H` of `run`'s canonical JSON, and
`{"kind": "receipt", "id", "run_id", "input_digest", "vector_digest"}`,
refused with `unknown-run` when it names a run VeGa was never told; each
connection is told its run before its first receipt. The answer is
`{"ok": true}` or `{"ok": false, "code": "unknown-run" · "conflict" ·
"invalid", "error": "<why>"}`. A line is at most 1 MiB.

*Reading the answer.* A base64 vector is its float32 bytes, a whole
number of four-byte components, as they are. A vector the runner writes
as JSON numbers is read only by the exact parser (`app/vega/client.py`,
`parse_response`), never by a general JSON reader: each number is the
binary32 nearest its decimal, rounded directly to binary32 and never
through a binary64 — through one, `7.038531e-26` and `-7.038531e-26`
land on their even neighbour, and of the shortest decimals that read
back to each finite binary32, which is what the runner (Go) writes,
these two are the only ones that do (exhaustive over all 4,278,190,080,
`tools/vega_f32_json.go`) — and `-0` is negative zero, which a general
reader takes for the integer 0. A vector that is not finite is not verified: it is the
engine's numeric failure, which the client asks again for. Louise then
reads the receipts of the batch and takes its vectors only when every
input has one, every input digest is the one she computed for what she
sent, and every vector digest is `H` of the bytes she received; the
receipts' run is the answer's run, held to 03 §3.3 before a write or a
query (two runs in one batch: each held to it). No receipt, VeGa
unreachable or the wrapper's `VeGa unavailable` is a deferral; a digest
that does not match, or a receipt or body that is not well formed, is a
refusal.

**The request's level.** Every request a node makes to a model endpoint
— an encoder, a reranker, the chat model, the translation model —
carries the request header `Louise-Level`, one of `corpus` ·
`translate` · `query` · `rerank` · `chat`, lowest first: the node's own
order of model work — corpus embedding, then a query's steps in their order,
so a query further along goes first (*amended 2026-10-25, operator*: the
same five values; the order was corpus · translate · chat · rerank · query).
It is advisory. RAGE uses it to hold the device's
clock while a person waits, and may use it for nothing an answer
depends on: the same request gives the same answer at every level, and
an endpoint that ignores the header loses nothing but speed.

**The fingerprint.** Its sixteen texts are fixed by the set's bytes
alone. The set is ordered by the UTF-8 byte length of `text` — a pair
set's by its two segments' together, and its sixteen are embedded as
pairs (*amended 2026-10-08*) — ties
broken by `i`, and the texts at ranks ⌊k × 511 / 15⌋ for k = 0 … 15 are
taken: the shortest, the longest and fourteen spaced between. They are
embedded one at a time, truncated as a chunk is. A run's fingerprint is
`H` over the sixteen vectors, in that order, each as the endpoint
returned it, converted to little-endian f32 and concatenated. The gate
embeds the same sixteen a second time as the peer contributes, with its
batching and concurrency. The run is batch-invariant at those settings
when the bytes are equal (03 §3.3, *amended 2026-10-25*; the band of
2026-10-12 is gone). Before that the gate embeds the sixteen one at a
time a second time, in reverse order; bytes that differ fail the run
(invariant 12). An asymmetric space's query run has its own
fingerprint, over the same texts with `space.query.instruction`
prefixed.

**The peer's calibration statement.** The member above is the space
author's reference. What a contributing peer produces when it passes
the gate (03 §3.3) is a different object, and the one every other
document means by "a compliant encoder": an envelope (§9.2) under the
domain `louise/calibration/v1`, kept in `state/` and published in this
peer's digest (§9.9), with body

```
{kind: "louise-calibration-run", version: 3 · 4,
 space_id, collection_id, set, role: "document" | "query",
 run: <run_id>, engine: <engine, §9.3>, encoder_weights, driver: <string>,
 fingerprint: <id>, batch_invariant: <boolean>,
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
rendezvous by.

`run` is the run identity the platform stated. `engine`, `encoder_weights`
and `driver` are what it stands for, written out as the Codex writes its
own engine (§9.3). The engine's `engine_id` is what the peer's bundles,
attestations and benchmarks carry. `role` says whether the run embedded
documents (against `vectors`) or queries (against `query_vectors`).
`fingerprint` and `batch_invariant` are the gate's own measurements
(03 §3.3); a run that is not batch-invariant is validated for
single-text, one-at-a-time work only.

A peer keeps one statement per run it has validated, and a vector is
written only from an answer naming a run with a passing statement for
the space (03 §3.3). Version 4 is this body whose `engine` is a backend
engine (§9.3), and is written for such a run alone: a run of any other
engine is stated at version 3, so no statement already held moves and
none is gated again (*amended 2026-10-08*, docs/notes/vega.md). Version 2 is this body with an `engine` that
cannot carry `options` (§9.3). A version-2 statement stays valid as
written and is read beside version 3: its run is unchanged, and gating
it again would recompute a verdict already held (operator, 2026-10-03).
Version 1 of this body carried `encoder_image`, the engine's image
digest alone, in place of the run, and had no fingerprint.

**Worked example.** The text set shipped as `calibration/6b1a3f0e9c2d4b7f.jsonl`
(`sha256:6b1a3f0e9c2d4b7f…`); the PubMed Codex names it, and its member
`calibration/6b1a3f0e9c2d4b7f.safetensors` holds `vectors [512, 768]`
and `envelope [2, 768]` with `space.calibration.vectors: "sha256:d4…"`
and `space.envelope: "sha256:77…"`. A sister of PubMed on a
domain-trained encoder names the same set and a different member.

## 9.13 Licence terms and the acknowledgement

**The terms member.** `LICENSE-TERMS` at the SHARD's root: the origin's
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

**The web-sources acceptance.** `state/web-sources-ack.json` under
`LOUISE_ROOT` (07 §7.8), local, never a member: one per node, recorded
by `hold collection ack --web-sources` (04 §4.4).

| field | type | required | meaning |
|---|---|---|---|
| `kind` | `"louise-web-sources-ack"` | yes | |
| `version` | `1` | yes | |
| `terms_sha256` | id | yes | `H` of the disclaimer text the platform ships, byte-exact |
| `time` | time | yes | when |

It is resolved by the same function as every acknowledgement: it
counts while its `terms_sha256` equals the hash of the text the running
platform ships, so a changed disclaimer asks again.

## 9.14 Relations

**Path.** `relations/<slice>/<NNNNN>.jsonl.zst`, one part beside each
record part of the same slice and number (05 §5.5), listed in the
snapshot's `relations` with its `root` (§9.4) and re-issued with its
record part. The file uses the framing of §9.5 with the index's `uids`
map keyed by `from` (the frame and line of the first edge of each
record).

**The edge line.** Compact JSON, keys in this order, `\n`-terminated:

| key | type | required | meaning |
|---|---|---|---|
| `from` | uid | yes | a record of this collection, in this part |
| `to` | string | yes | a uid of this collection, or a canonical id `<namespace>:<id>` left unresolved |
| `type` | string | yes | `<plugin>:<name>`, registered by the plugin: `eli:cites`, `eli:amends`, `eli:repeals`, `pubmed:cites` |
| `snapshot` | `YYYY-MM` or `YYYY-Www` | yes | the snapshot in which the edge was first listed |

**An annotations collection's edges** (*amended 2026-10-09*; 02 §2.10,
05 §5.5) are its claims, one per claim record, `from` = the claim's
`subject`, `to` = its `object`, `type` = `annotations:<claim type>`
(`annotations:same-work`, `annotations:translation-of`). Both ends are
uids of the collection the Codex's `annotates` names, so `from` is a record
of THAT collection and the part is the one holding the claim. The edge is
the annotator's explicit statement, which is what 05 §5.5 asks of an edge.

*Amended 2026-10-20* (A-K2, A-K3): the three claim types of 2026-10-20
make these edges, each in the part holding the claim. A `mention`: `from`
= the chunk's RECORD uid (a chunk is not a uid), `to` = the entity's
canonical id, `type` `annotations:mention`. A `relation`: `from` = the
subject entity's uid in the entity target (`wikidata:<native_id>` — an
entity is a record of a collection the Codex annotates, which is what
lets `from` be a record), `to` = the object's canonical id, `type`
`annotations:relation`; the property stays in the claim, not in the edge.
A `community` has no edge of its own: its members are edges `from` each
member's record uid `to` the community claim's own uid
(`annotations:<n>`), `type` `annotations:member-of`.

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

**Path.** `seals/<snapshot_id>.json` at the SHARD's root, certified
collections only (03 §3.7); canonical JSON; its transport root and size
are in the snapshot's `files`, which the certifier signs, so the
evidence is bound to the snapshot it was captured for. Beside it,
`seals/lists/<sha256>.xml` holds each trusted list — the national list and
the LOTL — that its entries name by hash, byte for byte, also in `files`
and in the same member: a peer that fetches the seals holds what archival
mode judges them against (*amended 2026-10-08*; the lists had stayed on
the certifier).

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
| `seal` | `{b64: string}` | yes | the signature envelope itself, DER as base64 — the CMS `SignedData` for CAdES and PAdES, the `<ds:Signature>` element for XAdES; DETACHED, its encapsulated content removed (*amended 2026-10-07*: the evidence stays small however large the issue, and archival mode verifies over `content_sha256`, not the bytes); for XAdES, the element's bytes, every namespace in scope declared on it and every `ds:Object` but the qualifying properties removed (*amended 2026-10-07*) |
| `content_sha256` | id | yes | the message digest the signature commits to, taken from the envelope's signed attributes. For XAdES, the `DigestValue` of the signature's ONE data reference, which must be SHA-256 (several data references, or another digest, are refused). Archival mode verifies the signature over `seal` and checks it commits to this value; it is NOT `file_hash`, which is BLAKE2b-128 of the sealed file (below) and is what live mode re-fetches against |
| `signer` | object | yes | `{subject, issuer, serial}` of the signing certificate, as strings |
| `chain` | list of strings | yes | the certificate chain, DER as base64, leaf first |
| `revocation` | list of `{kind: "ocsp" · "crl", b64: string, time}` | yes | the responses captured at validation — embedded in the envelope, given, or fetched by the certifier — kept only when they are about this entry's chain (an OCSP response naming one of its serials, a CRL issued by one of its issuers); `time` is the response's producedAt or thisUpdate. An entry that carries any is validated HARD-FAIL on revocation: every certificate of the path that declares a responder must have an answer among them, and a revoked one fails (*amended 2026-10-07*) |
| `timestamp` | `{b64: string, time}` or null | yes | an RFC 3161 token over the sealed bytes, when one was obtained |
| `trusted_list` | `{lotl_sha256: id, tl_sha256: id, url: string}` | yes | the List of Trusted Lists and the national list the chain validated against, hashed. A list is trusted for WHO SIGNED IT: at capture the LOTL's XAdES signature is verified with a certificate the Official Journal announced and the national list's with one the LOTL's pointer for its territory names, valid at its signing time; archival mode re-verifies the national list against the LOTL whenever both are held (*amended 2026-10-07*) |
| `verified_at` | time | yes | when the certifier validated it |

`origin-seal` in archival mode verifies the signature in `seal` over
the digest in `content_sha256` and validates its certificate path
against `chain`, `revocation` and `trusted_list`, with no network and
without the sealed bytes; in live mode it
re-fetches the sealed file, checks it hashes to `file_hash`, recomputes
from its bytes the digest the envelope commits to (the attached content of
a `.p7m`, a PDF's signed byte range, a XAdES document's data reference),
and validates now against the current lists — the current LOTL trusted
for a key the LOTL held names for itself, the current national list for
one the current LOTL's pointer names — and fresh revocation responses,
hard-fail; a list or a responder that cannot be had makes it
inconclusive, never a failure. The sealed files themselves travel
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
| `records-<slice>` | every `records/<slice>/<NNNNN>.jsonl.zst` (or `.parquet`, §9.5) of the current snapshot | per part, in the snapshot |
| `relations-<slice>` | `relations/<slice>/*.jsonl.zst` | per part, in the snapshot |
| `figures-<slice>` | `figures/<slice>/*.fig.zst` | per part, in the snapshot |
| `bundle-<space>-<bundle_id hex>` | the four bundle files | member root, in the bundle event and the digest |
| `vectors-<space>-<slice>`, `buckets-<space>-v<n>-g<k>` | names only: resolve to the bundles covering those chunks (04 §4.3) | — |
| `centres-<space>-v<n>` | `vectors/<space>/centres/v<n>.safetensors` | in `index.json` |
| `index-<space>` | `vectors/<space>/index.json` | in the digest |
| `calibration-<set>` | `calibration/<set>.safetensors` and `.sig.json` | in the snapshot's `files` |
| `validation` | `validation.json` | in the snapshot's `files` |
| `terms` | `LICENSE-TERMS` | in the snapshot's `files` |
| `slicing` | `slicing/<sha256 hex>.json`: the family map a slicing block names (§9.3; *amended 2026-10-24*) | in the snapshot's `files` |
| `seals-<snapshot_id>` | `seals/<snapshot_id>.json`, and `seals/lists/<sha256>.xml` for each trusted list its entries name (*amended 2026-10-08*) | in the snapshot's `files` |
| `origin-<slice>` | the sealed origin files of a certified collection, optional | per file, in the digest |

No two kinds share a shape (the text index's `index-<slice>`, which shared
`index-<space>`'s, left the table on 2026-10-08), and a slice name of 16
lowercase hexadecimal characters stays refused (§9.3).

A member of one file has that file's pieces root as its `root` (§9.5).
A member of several files has a root per file, and its member root is
`H` over the canonical JSON of the list `[[path, root, bytes], …]` in
path byte order; the digest lists the files.

**Torrents.** Every file of a member is one BitTorrent v2 torrent
(BEP 52), and the torrent is a function of the file's path, size and
root alone, so two honest publishers produce one infohash. The info
dictionary contains exactly: `meta version: 2`, `name` = the member
name, `piece length: 16777216`, and `file tree` with the one file at its
SHARD-relative path carrying `length` and `pieces root` — for a bundle,
which deliberately rests outside the SHARD (§9.7), the path is relative to
the bundle directory instead, so its member root is a function of the
bundle alone; no `private`
flag, no comment, no creation date, no creator, no announce list — the
swarm is found under the snapshot's swarm key and by the digests'
`peer_port`, never by the infohash in the DHT (below). The infohash is
SHA-256 of the bencoded info dictionary, truncated to 20 bytes where the
DHT needs 20; the piece layers are exchanged in the swarm. A member of
several files is that many torrents. Nothing is served from a torrent
that is not in the SHARD, in `state/bundles/` (§9.7), or in the
platform's encoder store (04 §4.3), and the same key that signs digests
seeds them.

**DHT keys.** The mainline DHT (BEP 5). A collection's key for a
snapshot is the first 20 bytes of `H(collection_id ‖ snapshot_id)`; the
lineage key is the first 20 bytes of `H(lineage_id ‖ snapshot_id)`; an
encoder's key is the first 20 bytes of `H` of its weights digest, under
which any peer holding those weights announces, whatever collections it
holds (04 §4.3); an engine's is the first 20 bytes of `H` of its
`image` digest, under which any peer holding that image announces; a
sharer announces its peer-message port under both for every snapshot it
serves (04 §4.2). What a searcher finds there is an address that speaks
§9.9. The swarm key is the first 20 bytes of
`H(collection_id ‖ snapshot_id ‖ "swarm")`, under which a sharer announces
its swarm's port for every snapshot it serves; what is found there is
where the snapshot's member torrents are served. No member torrent is
announced by its own infohash (amended 2026-10-19, 04 §4.2).

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
| `delta` | `{mode: "updatefiles-dir" · "dated-dumps" · "full-resync" · "zip-directory", subdir: string or null}` | yes | `zip-directory` (*amended 2026-10-12*, docs/notes/normattiva-multivigenza-review.md §3.10): a zip the origin rebuilds in place is brought up to date by its own central directory — read by Range, compared member by member on CRC-32 and size — and one Range request per changed member; the local copy is rebuilt from the members it holds and the ones fetched, raw bytes copied, never recompressed. The members decompress to the origin's bytes, which is what `origin_hash` covers; the file itself is not byte-identical to the origin's, which no checksum declares. An origin that will not serve ranges, a member that does not check out, or a delta that would move more than half the file falls back to the whole fetch |
| `endpoints` (mirrors) | | no | additional endpoints, regions, connection limits |
| `etiquette` | | no | `{max_conns, bandwidth_cap_bps, windows}` |
| `profiles`, `profile_mode`, `profile_labels`, `estimated_bytes` | | no | named selections of slices and their sizes |
| `all_profile` | string or null | no | the profile `slices = "all"` spells (07 §7.8) where it is not the one whose filter includes everything — a spec that lists files beyond a collection's whole names it (*amended 2026-10-13*: Normattiva's `full` is the 23 pre-packaged collections; `archive` adds the yearly exports for another collection) |
| `slice_profiles` | map of slice name to profile | no | the slices the origin delivers whole, each with the profile that fetches it — `lang=it` → `it` for Wikipedia's dumps; a slice under one (`lang=it/pages=3M`) is fetched as it, and an origin whose every file may hold any slice declares none (*amended 2026-10-25*: `records fetch --slice`, 07 §7.3) |
| `probe_file` | | no | the mirror structure probe |
| `extras` | | no | formats the origin offers that Louise does not exchange |
| `terms_text_path`, `ssh_key_path`, `auth_token_path` | paths | no | never in any shared object |

**Files the origin builds on request** (*amended 2026-10-13*,
docs/notes/normattiva-full-corpus.md §2–4). A declared file whose
request is not a URL but a job: its `remote` (local half, never in the
Codex) is `export:<partition>` — canonical JSON naming what the origin is
asked to build, `{"year": 1990, "form": "O"}` for Normattiva's asynchronous
export of a year of acts in one form — and its `rel_path` is where the
zip lands, `akn/E/<form>/<year>.zip`. The platform starts the job,
confirms it, polls its status and downloads the zip it names
(app/acquire/export_job.py); the file is listed without size or date, like
a rebuilt zip, and every pass asks the origin's UPDATE FEED, read since
the copy's own mtime, whether an act of the partition moved — the copy is
adopted when none did, re-exported when one did, left standing when the
feed cannot be read. The `files` entry in the Codex is the same
`{rel_path, size or null, checksum or null}` as any other.

**The deposit plugin's spec.** A deposit collection (02 §2.9) has no
acquisition spec: its origin is the client. Its `origin.spec` is exactly
`{"nonce":"<32 lowercase hex digits>","source_id":"deposit"}` — 128
random bits chosen at `create` — and none of the fields above. The
nonce is what gives two deposit collections created from the same
defaults two identities.

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
`sections`, `figures`, `origin_hash`; the platform adds `lang` (from
`language`),
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
(06 §6.1); `protocol_version` is `2`, and `relations()` is part of it:
the protocol is fixed with this version and not amended into it. Version
1 is this protocol without `figures` on `CanonicalDoc` and without the
`Figure` type below; a plugin that yields no plates implements 2 and
returns `[]`, because the protocol is what the platform may CALL and not
what a particular source happens to carry.

`Figure`: `{hash: str, media_type: str, bytes: int, seq: int, data:
bytes}` — what `parse()` yields for each plate. `data` is the only field
that does not reach the record line: the platform writes it into the
figures container (§9.5) and keeps the other four.

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
| `louise-codex` | 9 | §9.3 (5 since 2026-10-08: a layer's `input`; 6 since 2026-10-08: a backend engine, and written for a Codex that carries one alone — every other is written at 5; 7 since 2026-10-09: `annotates`, written for an annotations Codex alone, 02 §2.10; 8 since 2026-10-18: `space.cut`, written for a space that declares the token cut alone; 9 since 2026-10-20: `slicing.container`, `annotates` as a list, the claim types `mention`, `relation`, `community` — written for a Codex that declares one of them alone) |
| `louise-snapshot` | 2 | §9.4 |
| `louise-snapshot-sig` | 1 | §9.4 |
| record line | 2 | §9.5 |
| part index (`LOUISE-PACK-IDX1`) | 1 | §9.5 |
| figures container index (`LOUISE-FIGURES-1`) | 1 | §9.5 |
| `louise-index` | 1 | §9.6 |
| bundle header | 2 | §9.7 |
| `louise-attestation` | 3 | §9.8 |
| `louise-digest` | 2 | §9.9 |
| `louise-event`, `louise-pull` | 1 | §9.9 |
| `louise-certifier`, `louise-pointer` | 1 | §9.11 |
| `louise-calibration` | 3 | §9.12 (3 since 2026-10-08: a pair space's member carries its set, `pairs`; every other member's body is still written at 2) |
| `louise-calibration-run` | 4 | §9.12 (4 since 2026-10-08: a backend engine's run; every other run is stated at 3) |
| `louise-ack`, `louise-trust` | 1 | §9.13, §9.2 |
| `louise-seals` | 1 | §9.15 |
| VeGa tag (`\x1eVEGA/1\x1e`), receipt, receipts route | 1 | §9.12 (the version is in the tag's marker; neither the receipt nor the routes carry `kind` or `version`) |

Two rows above were one row each until the figures amendment, and the
split is the point: a schema pinned beside another moves only when its
OWN bytes move. `louise-snapshot` gained a `figures` list and
`louise-snapshot-sig` did not; the record line gained a `figures` key
and the part's frame layout did not. `louise-digest` left `louise-event`
and `louise-pull` the same way, when its benchmarks began naming an
engine rather than an image — the engine amendment, which moved the
Codex, the bundle header, the attestation and both calibration objects
with it. The options amendment (2026-10-03) moved only the two objects
that carry an engine WHOLE, the Codex and the peer's calibration
statement; the bundle header, the attestation, the digest and the
space author's calibration member carry an `engine_id` and kept their
versions. The input-forms amendment (2026-10-08) moved the Codex, for a
layer's `input`, and the space author's calibration member of a pair
space alone, which carries the pairs its vectors were produced over: its
body is written at 3, every other at 2, so no text space's member or
signature moved, and the peer's statement kept its version — its
`space_id` already says which form was measured. The VeGa amendment
(2026-10-08) added no key: a backend engine has the keys every engine
has, and is told by its bytes (`${BACKEND}` in its program, a tree
digest in `image`). It moved the two objects that carry an engine
WHOLE, the Codex and the peer's calibration statement, because a reader
of the old version would take a tree digest for an image and guess, and
writes the new version for an object carrying a backend engine alone:
every Codex, statement, `engine_id` and run already written or stated —
an argv engine's, one RAGE stated — keeps its bytes, and nothing is
gated again for it. The bundle header, the attestation, the digest and
the space author's calibration member carry an `engine_id` and kept
their versions. `protocol_version` (§9.3, §9.17)
is 2 and is not in this table: it versions the plugin contract, which
is a boundary inside one platform rather than a byte on the wire.

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
