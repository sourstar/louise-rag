# Step 4 — `hold vectors build | import | export | check`: what it must wrap

*Written 2026-09-08, on the `louise` branch, before any step-4 code.*
Eight agents read the specification and the existing `app/bundles/`,
`app/verify/` and `app/spaces/` against each other. This is their record:
what the documents require, where they disagree with each other, where the
code disagrees with them, and what does not scale. Nothing here is a
decision — the decisions are marked and belong to the operator.

**Read this before touching `app/bundles/` or `app/verify/`.** The headline
is that step 4 is NOT a wrap-and-adapt job the way steps 0–3 were: every
tier of the admission ladder disagrees with docs/03 §3.5 in substance
rather than in wording, and the bundle header is a closed schema that
today's writer and today's parser both violate.

## The five findings that change the plan

1. **`possession.json` carries the wrong hash.** The code maps uid →
   `raw_hash` (the origin bytes' BLAKE2b-128, i.e. 09 §9.5's `origin_hash`);
   09 §9.7 requires `content_hash`. Both are 32 hex characters, so the two
   are indistinguishable on the wire and every cross-import silently refuses
   every record. It is also unimplementable for PubMed as written: a
   red-class pointer record has no origin bytes on the importing peer.

2. **The blast radius of a possession mismatch is a three-way conflict.**
   09 §9.7 says one mismatch refuses the BUNDLE; 03 §3.5 and 07 §7.3 both
   say it refuses that RECORD'S ROWS and exits 0. Two documents against one.
   It is also the hinge of the figures decision, which was priced against
   the harsher reading — about 2.5 orders of magnitude apart for a 300-uid
   bundle. **Operator decision.**

3. **`app/bundles/trust.py` implements trust-on-first-use** and refuses
   every producer key after the first. 03 §3.5 and 08 §8.1 forbid this in as
   many words: no producer key need be known, trusted or seen before.

4. **`format.py` refuses every conformant bundle by construction** —
   `challenge` is forced to null in both directions where §9.7 makes it a
   required object — and five header fields are missing, three more are in
   the wrong textual form, and one (`backend_fingerprint`) is not in the
   specification at all. A `version` bump is the honest way out (§9.18).

5. **The PoC cannot import gutenberg's existing vectors honestly, and the
   corruption will not fall out at import.** 09 §9.7 requires
   `encoder_image`/`encoder_weights` "equal to the Codex's, or a build
   attested compliant"; the Lance table stores no per-row build identity and
   those rows were written by at least three builds, one of them the
   unapproved b10630. At the measured 8 % bad fraction, T3's sixteen-chunk
   quorum fires with probability ~0.006. **Sweep before importing, not
   after.**

## What was already fixed by finding it

The study's bundle-format dimension found the calibration member's path
using the full 64-hex digest where 09 §9.1 fixes a 16-digit segment. That
was found and fixed independently the same day (`drag.calibration_paths`,
`drag.calibration_member`, `app/louise.py`'s gate path, and the test that
had enshrined it). Two dimensions also named the missing calibration-member
WRITER as "step 4's first blocker"; that landed the same day as
`calibration.build_member` / `write_member` and `create`'s embedding pass.



## bundle-format â the bundle: its four files, its signature, its id

Study of the bundle (09 §9.7) for migration step 4, against §9.6, §9.12, §9.16, 03 §3.5, 04 §4.3 and the existing `app/bundles/`. The existing code is a good skeleton with an excellent security posture (verify the wire bytes, never re-serialise; framed domain-separated payload) but its bytes are NOT §9.7's: five header fields are missing, three are the wrong type or textual form, `challenge` is forced to null, and — the most valuable finding — `possession.json` carries the wrong hash entirely (`raw_hash` of the origin bytes, not the record's `content_hash`), which is both a wire-format break and an O(fetch-per-document) cost the spec's version does not have. Three genuine document defects found: (a) 09 §9.7 says a possession mismatch "refuses the bundle at T0" while 03 §3.5 and 07 §7.3 both say it refuses only that record's rows and exits 0 — a three-way conflict that changes the price of the figures decision by ~2.5 orders of magnitude; (b) §9.7's own worked example of `uids` is not in byte order and its explanatory sentence states the sort backwards; (c) 07 §7.3 spells `export --out FILE` / `import <file>` for an artefact 09 §9.7 defines as a directory of four files, and no container format is fixed anywhere. The size-critical items are the per-bundle full scan of the Lance table in export, the per-uid `plugin.fetch()` in possession, and the O(shard) rewrite of the at-rest safetensors on every accepted bundle.

### disagreements

1. **problem**: A THREE-WAY CONFLICT on the blast radius of a possession mismatch, and it is the single most consequential open question in step 4. 09 §9.7: "The importer compares every entry with its own record; ONE MISMATCH REFUSES THE BUNDLE at T0." 03 §3.5: "a record whose `content_hash` in the bundle differs from the one in this peer's copy has ITS ROWS refused — the record was re-issued since — with the refusal recorded, never counted as a failure." 07 §7.3: "A record whose `content_hash` in `possession.json` differs from this copy's has THAT RECORD'S ROWS refused and the refusal recorded … and the import EXITS 0, never a failure." The current code implements 09's reading — `possession.verify_map` (app/bundles/possession.py:87-107) raises and `import_bundle` returns `{"ok": False}`, importing nothing. At 36M PubMed records, where abstracts are revised continuously, whole-bundle refusal means one revised abstract voids the other 299 records' vectors and the network re-embeds them; the per-record reading costs one record. This is also exactly the hinge of the figures decision: docs/notes/figures-in-the-corpus.md quotes this very paragraph to argue a figure must not enter `content_hash` because moving it "orphans that record's bundles NETWORK-WIDE". Under 03/07's reading the damage is one record's rows; under 09 §9.7's it is every bundle that contains that record — for a 300-uid bundle, ~2.5 orders of magnitude more re-embedding. The decision recorded in that note was priced against the harsher reading.
   **suggestion**: Implement 03 §3.5 and 07 §7.3: refuse that record's rows, import the rest, record the refusal, exit 0. Two documents against one, and it is the only behaviour that survives the corpus sizes. Then amend 09 §9.7's sentence — it reads as shorthand for "refused at the T0 stage" rather than "refuses the whole bundle", but it is the byte-fixing document and must not be left ambiguous. Keep whole-bundle refusal for `space_id` ("refused at T0, before any tier runs") and for a uid in `uids` with NO possession entry at all, which is a malformed bundle rather than a stale record. Re-price the figures note's first section against whichever reading the operator ratifies.
   **where**: 09 §9.7 line 824 vs 03 §3.5 ("a record whose `content_hash` in the bundle differs … has its rows refused") vs 07 §7.3 lines 238-243

2. **problem**: `possession.json` CARRIES THE WRONG HASH. The spec maps uid -> the record's `content_hash` (§9.5: BLAKE2b-128 of the canonical JSON of `[title, [[heading, text], …]]` — the cleaned text as the chunker sees it). The code maps uid -> `raw_hash`, the plugin's blake2b-128 of the RAW ORIGIN BYTES, which in spec vocabulary is `origin_hash`, not `content_hash`. Both are 32 hex characters, so the two forms are indistinguishable on the wire and `parse_possession`'s `_check_hex(v, ..., 16)` accepts either — a legacy bundle and a conformant bundle both parse, and every cross-import silently refuses every record. Worse, the two hashes are not even equivalent in meaning: two origin files that clean to identical text have different `raw_hash` and identical `content_hash`, so the code refuses vectors that are perfectly valid (a re-fetched Gutenberg `.txt` with a changed boilerplate footer); and a red-class POINTER record has no origin bytes on the importing peer at all, so `raw_hash_for` cannot even be computed there — the possession check is unimplementable for PubMed as specified in the code.
   **suggestion**: Change the value to `records.content_hash(title, sections)` and store it as a column on `documents` when the record line is built (step 2), so `verify_map` is a single indexed `SELECT uid, content_hash FROM documents WHERE uid IN (...)` and never touches a plugin. Delete `raw_hash_for`'s fetch-and-backfill path from the bundle code entirely (`txt_hash` keeps its own separate job in discovery). This also removes the possession module's dominant cost.
   **where**: app/bundles/possession.py:49-84 vs 09 §9.7 lines 769, 821-826

3. **problem**: FIVE HEADER FIELDS MISSING, ONE EXTRA, and the header is a closed schema in both directions — so no conformant bundle parses today and no bundle written today is conformant. Missing: `snapshot_id`, `shard`, `encoder_image`, `encoder_weights`, and a non-null `challenge`. Extra: `backend_fingerprint`, which appears nowhere in 09 and is described in export.py:193-201 as "advisory metadata for a later tolerance decision" from `architecture-v2.md §6` — an engineering-record document, not the specification. Its loss matters: `app/verify/t2.py:115` and `t3` select tau by matching `backend_fingerprint` against calibration `pairs`, so removing the field removes that dispatch, which is correct (03 §3.1 forbids it) but must be replaced by `space.tolerance`.
   **suggestion**: Rewrite `_HEADER_KEYS` to §9.7's sixteen. `shard` has no source in the current manifest — step 2's shard assignment must expose `uid -> "<slice>/<n>"` and `hold vectors build --shard` must take it, since "one shard per bundle" is a hard constraint. `encoder_image`/`encoder_weights` come from the Codex's `space.encoder`; note the migration plan already records two operator blockers here (dnabert2 has no RepoDigest, llama.cpp serves no `/info`), so `encoder_weights` may be a weak hash on this node.
   **where**: app/bundles/format.py:102-107 vs 09 §9.7 lines 782-803

4. **problem**: THREE FIELDS IN THE WRONG TEXTUAL FORM, all silently well-formed. `space_id` is `_check_hex(..., 16)` = a bare 32-hex BLAKE2b-128 (app/spaces.py:222-239 uses `hashlib.blake2b(blob, digest_size=16)`), where §9.3/§9.1 fix an `id` = `"sha256:" + 64 hex` and app/hold/codex.py's `space_id` property already produces that. `content_digest` is bare 64 hex where §9.7 says "written `sha256:…`". `producer_pubkey` is bare 64 hex where §9.2 fixes `ed25519:` + 64 lowercase hex. `collection` carries `collection.name` (export.py:161) — a peer's local word for the collection — where §9.7 says `id`, i.e. the Codex's `collection_id`. And the whole `spaces.compute_space_id` payload is a different function from `codex.space_id` (different inputs, different hash), so the value is wrong as well as the form.
   **suggestion**: Take every one of these from the Codex: `space_id` = `codex.space_id`, `collection` = the collection id, `encoder_*` = `codex.space.encoder`. Add prefixed-id and prefixed-key checkers to format.py (envelope.py:63-72 already has `key_bytes` for the `ed25519:` form — reuse it). This is a hard wire break from every bundle this node has ever written; the migration plan's step 6 imports legacy vectors with producer `legacy-local`, which is the right escape hatch — do not add a compatibility path to the parser.
   **where**: app/bundles/format.py:300, 306, 308 vs 09 §9.1 / §9.2 textual forms

5. **problem**: THE SPEC'S OWN WORKED EXAMPLE IS NOT IN BYTE ORDER, AND ITS EXPLANATION STATES THE SORT BACKWARDS. The header example lists `["gutenberg:10102","gutenberg:10125","gutenberg:1012","gutenberg:10131",…]` and the prose says "`uids` is sorted by bytes, which is why `gutenberg:1012` FOLLOWS `gutenberg:10125`". Under byte order a prefix sorts BEFORE its extension, so `gutenberg:1012` < `gutenberg:10125`; the correct order is `10102, 1012, 10125, 10131`. The surprising fact the sentence was reaching for is that `gutenberg:1012` follows `gutenberg:10102` (`'0'` < `'2'` at the fourth digit). An implementer who takes the example as normative writes a sorter that puts extensions before prefixes and produces bundles that fail every other peer's `list(uids) != sorted(uids)` check. (The `possession.json` line immediately below, `{"gutenberg:1012":…,"gutenberg:10102":…}`, is also not in sorted-key order and disagrees with the header's order as well.)
   **suggestion**: Correct both the array and the sentence in 09 §9.7. Python's `sorted()` on `str` is code-point order, which for UTF-8 is byte order, so `sorted(uids)` is already right in format.py:295 — the code is correct and the document is not.
   **where**: 09 §9.7 lines 852-864 (the worked example)

6. **problem**: DOCUMENT-VS-DOCUMENT: 07 spells `vectors export <collection> [--shard <slice>/<n>] --out FILE` and `vectors import <collection> (<file> | --archive ID …)` — a single FILE — for an artefact 09 §9.7 defines as "a directory of four files" and says "`hold vectors export` writes the same directory anywhere, `hold vectors import` reads it". No container format (tar? zip? uncompressed? what member names?) is fixed anywhere in 09, which is the document whose job is to fix "every byte that is hashed, signed or moved".
   **suggestion**: Read `--out` as a DIRECTORY and make the argument name say so (`--out DIR`), matching §9.7 and matching what export.py already does (`dest.mkdir(parents=True, exist_ok=True)`). Ask the operator to amend 07's spelling. If a single-file form is genuinely wanted for hand-carrying, it needs a fixed container in §9.7 with fixed member names and ordering, because the parquet's SHA-256 is signed and any repacking that touches bytes breaks the signature.
   **where**: 07 §7.3 lines 92-94 vs 09 §9.7 lines 762-774

7. **problem**: THE CALIBRATION MEMBER'S PATH USES THE FULL 64-HEX DIGEST WHERE THE SPEC FIXES A 16-DIGIT SEGMENT. §9.1: "Where a digest names a directory — the `<space>` segment under `vectors/`, THE `<set>` SEGMENT OF THE CALIBRATION MEMBER (§9.12) — the segment is the first 16 hexadecimal digits of the digest after the prefix." §9.12's worked example: `calibration/6b1a3f0e9c2d4b7f.jsonl` for `sha256:6b1a3f0e9c2d4b7f…`. `drag.calibration_paths` returns `calibration/<full 64 hex>.safetensors`, `drag.calibration_member` returns `calibration-<full 64 hex>`, `app/louise.py:274` builds the same 64-hex filename, and `tests/hold/test_drag.py:100` asserts `"calibration/" + "6b"*32 + ".safetensors"` — the divergence is enshrined in a test. This is in my dimension because T2's reference vectors are read from exactly this path: two conformant peers would look in different places, and the member root that goes in the snapshot's `files` is over a differently-named path.
   **suggestion**: Use `calibration.segment(set_id)` (app/hold/calibration.py:75-77, which already implements the 16-digit rule) in both `calibration_member` and `calibration_paths`, and fix the test. `drag.check_space` already enforces 16 hex for the space segment, so the two are inconsistent with each other today.
   **where**: app/hold/drag.py:109 and 174-176, app/louise.py:274, tests/hold/test_drag.py:97-100 vs 09 §9.1 ("Path segments from digests") and §9.12

8. **problem**: WHERE BUNDLES LIVE IS GIVEN AS A BARE RELATIVE PATH. §9.7: "On a peer, accepted bundles live at `bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/`" — relative to what is not said, and the drag root is the natural reading. But 07 §7.8's tree puts `bundles/` inside `state/`, and 07 §7.8 says a bundle is "served from `state/bundles/` (09 §9.7, §9.16)"; §9.16 says "Nothing is served from a torrent that is not in the drag, in `state/bundles/` (§9.7), or in the platform's encoder store". Also 07 §7.8: "`share` refuses a drag directory holding a file that neither the Codex names, nor a snapshot of the chain lists … nor 09 §9.6 defines as a local derived file under `vectors/`" — a bundle is none of those, so putting it in the drag would make `share` refuse the whole drag.
   **suggestion**: `collections/<name>/state/bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/`. Worth one clarifying word in §9.7. Note the knock-on: §9.16's torrent info dictionary specifies "the one file at its DRAG-RELATIVE path", which is meaningless for a file that is deliberately not in the drag — the four bundle files' torrent paths are unspecified. Suggest the bundle-directory-relative names (`header.json`, `vectors.parquet`, `possession.json`, `MANIFEST.sig`), which also makes the member root a function of the bundle alone.
   **where**: 09 §9.7 line 773 vs 09 §9.16 lines 1382-1385 and 07 §7.8 line 665

9. **problem**: THE EXISTING T2 IS A DIFFERENT PROTOCOL, not an approximation of this one: 8 beacon-drawn indices instead of 16 fixed ones, vectors in the header instead of the parquet, a golden bank in a `space artifact` instead of the calibration member, a 30-day freshness window where 03 §3.5 says "no age rule", and `tau_default = 0.995` — a constant threshold, which 03 §3.1 forbits in as many words ("No number in this paragraph is a threshold: `tolerance` comes from calibration and from nowhere else"). The migration plan already made this call for step 3's `backend_gate` ("It is replaced, not wrapped"); the same verdict applies here and the plan's step-4 text does not say so.
   **suggestion**: Replace `app/verify/t2.py` with a T2 that reads rows 0..15 of the parquet, upcasts to f32, cosines against rows 0..15 of the calibration member's `vectors` tensor, and passes on the Codex's `space.calibration.quorum` against `space.tolerance`. Carry over its docstring's honest limit (the challenge set is public and precomputable, so a T2 pass must never substitute for T3) — §9.7 and 03 §3.5 agree with it.
   **where**: app/verify/t2.py:97, 137, 155 vs 09 §9.7 line 803 and 03 §3.1

10. **problem**: `challenge` IS FORCED TO NULL IN BOTH DIRECTIONS: `to_wire` always emits `"challenge": None` and `parse_header` raises "header carries challenge data, which this version cannot verify" on anything non-null. §9.7 makes `challenge` a REQUIRED OBJECT `{set: id, n: 16}`. So today's parser refuses every conformant bundle by construction, and today's writer produces a bundle whose T2 can never run.
   **suggestion**: This is intentional (a P1.1/P1.2 staging decision) and simply lands with step 4 — but it means the format version must move: a v1 bundle and a §9.7 bundle disagree on a required field's type. Either bump `version` to 2 or accept that `version: 1` means §9.7's shape from here on and that every previously exported bundle is unreadable. Ask the operator; §9.18 ("A change to any schema here is a new `version`, never a silent extension") points at the bump.
   **where**: app/bundles/format.py:275-281 and 228


### existing_code

1. **does**: The v1 header schema and strict parser. `_HEADER_KEYS` (lines 102-107) is 16 keys: version, space_id, collection, uids, chunk_count, dim, dtype, producer_pubkey, backend_fingerprint, content_digest, parquet_bytes, possession_bytes, created_at, digest_alg, sig_alg, challenge. Closed in both directions (lines 260-265). Rejects floats, duplicate keys, lone surrogates, non-NFC strings, bools-as-ints; caps at 1 MiB / 4096 chars / 10^6 uids / dim 8192.
   **reusable**: Yes — the parsing discipline is exactly right and should be preserved wholesale. Only the key set and the value forms change. Rename `to_wire`'s payload dict, add the five missing keys, drop `backend_fingerprint`, and switch the three `_check_hex` calls to prefixed-id / prefixed-key checkers.
   **where**: app/bundles/format.py:82-107, 191-312

2. **does**: Byte-for-byte §9.7's payload: `_SIG_DOMAIN = b"louise-rag/bundle-sig/v1\x00"` then `_framed(header_bytes) + _framed(parquet_sha256) + _framed(possession_sha256)` with `struct.pack(">I", len(part))`.
   **reusable**: Yes, unchanged. The ONE deviation is cosmetic: `bundle_id_of` returns bare hex where §9.7 writes `"sha256:" ‖ hex(...)`. The directory name wants the bare hex, the event and ledger want the prefix — add a second accessor rather than changing this one.
   **where**: app/bundles/format.py:349-370 (`signed_payload`, `bundle_id_of`)

3. **does**: `_VECTORS_DOMAIN = b"louise-rag/vectors/v1\x00"`, `struct.pack(">II", dim, len(rows))`, then per row `u32be(len(utf8(chunk_id))) ‖ chunk_id ‖ vec.astype('>f2'|'>f4').tobytes()`. `sort_rows` keys on `(uid.encode('utf-8'), int(seq))`.
   **reusable**: The digest matches §9.7 exactly, including big-endian pinning and the tuple sort. But `uid_of` (line 443) does `chunk_id.rsplit(':', 2)` and REQUIRES three parts — it raises `BundleFormatError` on `"calibration:0"`. So `sort_rows` and `content_digest` as written cannot process a spec-conformant parquet at all: the challenge rows must be split off before sorting and concatenated back at the head.
   **where**: app/bundles/format.py:399-430 (`content_digest`) and 433-459 (`sort_rows`, `uid_of`, `seq_of`)

4. **does**: Writes the four files today: `_write_parquet` with pinned pyarrow options (version 2.6, no compression, no dictionary, no statistics, no page index, `store_schema=False`), then `possession.json` via `dump_possession`, then `header.json` via `header.to_wire()`, then `MANIFEST.sig`. Header fields come from `con.execute("SELECT space_id FROM space WHERE id=1")`, `collection.name`, `covered` uids, `len(rows)`, Lance's `dim`/`dtype`, `public_key_hex(key)` and `_backend_fingerprint()`.
   **reusable**: The file-writing skeleton and the pinned parquet options are worth keeping verbatim. Everything about WHAT goes in the header changes. Note there is no `shard` concept anywhere in this module — it exports by uid list, and §9.7's "one shard per bundle" has no representation.
   **where**: app/bundles/export.py:124-190 (`export_bundle`)

5. **does**: One `SELECT native_id FROM documents WHERE uid=?` per uid, then `table.search().where(f"book_id IN ({ids})").limit(10_000_000).to_arrow()` against Lance.
   **reusable**: Not as-is at corpus scale — see size_risks. Rewrite as a single manifest query and a shard-scoped Lance read.
   **where**: app/bundles/export.py:59-103 (`_native_ids`, `_read_vectors`)

6. **does**: `raw_hash_for` returns `documents.txt_hash` when present, otherwise calls `collection.plugin_instance().fetch(DocRef(...))` and uses `raw.raw_hash` (blake2b digest_size=16 of the RAW ORIGIN BYTES), backfilling the manifest. `build_map` does that per uid; `verify_map` raises `PossessionError` naming the offending documents when ANY uid mismatches or is unclaimed.
   **reusable**: The shape (per-uid map, named offenders) is right; the hash is wrong (see disagreements) and so is the acquisition path. Under §9.7 the value is the record's `content_hash`, which the record line already carries, so the whole fetch-and-backfill machinery disappears and is replaced by an indexed lookup.
   **where**: app/bundles/possession.py:49-107

7. **does**: The gate order signature -> header -> trust -> space -> content digest -> possession -> local chunk agreement -> ladder -> coarse checks -> write. `_read_part` (line 67) refuses a file whose size differs from the signed `parquet_bytes`/`possession_bytes`. `_read_parquet` (line 140) asserts the column set is exactly {chunk_id, vector} and `len(chunk_ids) == header.chunk_count`. Writes with `embed_done=0` before touching Lance, then delete-then-insert per uid under `vector_write_lock`, then `embed_done=1`, `DELETE FROM embed_queue`, ledger rows.
   **reusable**: The gate ORDER, the crash-safety ordering (lines 17-28, 450-455) and the never-raise-to-the-caller contract are all correct and should carry over unchanged. Three edits: the row-count assertion becomes `chunk_count + challenge.n`; the challenge rows must be sliced off before `uid_of` is ever called; and the possession failure must become per-record, not whole-bundle (see disagreements).
   **where**: app/bundles/importer.py:333-490 (`_import_checked`)

8. **does**: A completely different T2: challenge INDICES derived from a beacon (`HMAC-SHA256(root, "louise-rag/beacon/v1" ‖ u64be(created_at // epoch_seconds))`), default `challenge_count = 8`, indices drawn mod `bank_size` from a signed space artifact's golden bank, a freshness window (`max_bundle_age_seconds`, default 30 days) and a clock-skew check, and a `tau` from the artifact's calibration table with an f16 storage penalty and an advisory floor of 0.95.
   **reusable**: No — it is superseded, not wrapped. §9.7/§9.12/03 §3.5 fix a DETERMINISTIC set (texts 0–15 of the calibration set), n = 16 not 8, the reference is the calibration member not a `space artifact`, the pass rule is the calibration quorum against `space.tolerance` not a `tau_default = 0.995` constant (which 03 §3.1 forbids), the vectors live in the PARQUET not in the header, and there is explicitly "no age rule" so the whole beacon/freshness apparatus goes. `challenge_indices` and `_tau_for` have no counterpart in the spec. Its honest-limit docstring (lines 28-37) remains true and is worth carrying into the new module's docstring.
   **where**: app/verify/t2.py (whole file)

9. **does**: Coarse admission checks that always run: structural (dim/dtype/norm/NaN/dupes), an isotropy test against the uniform-on-the-sphere null, an adjacent-chunk overlap test with a 3-sigma bar, and an 8-chunk local re-embed at `MODEL_IDENTITY_FLOOR = 0.95`.
   **reusable**: Partly. The structural check is T0's first half. The isotropy and overlap tests have no place in 03 §3.5's ladder as written (T1 is a cosine profile against the calibration `vectors` plus a fixed-layer overlap band measured on LOCAL text at the 1st-99th percentile, not against the bundle's own baseline). `SAMPLE_SIZE = 8` must become sixteen (T3: "sixteen chunks chosen by the importer's private randomness … fails when fewer than twelve reach `tolerance`") and the 0.95 constant must become the Codex's `space.tolerance`.
   **where**: app/bundles/checks.py:125-151 and the isotropy/overlap/sample_reembed gates

10. **does**: The pre-embedded-archive import: an `ARCHIVES` registry with `credit()` producing 09 §9.3's credit object, per-shard admission with a quorum re-encode (`RE_ENCODE_SAMPLE = 16`, `RE_ENCODE_QUORUM = 0.75`, `RE_ENCODE_TAU = 0.95`), a `proven_at` per-archive proof, staleness skipping, and one `genesis_imports` row per shard as cursor and audit record.
   **reusable**: The registry, the `credit()` mapping and the archive->keys lookup that 03 §3.5 requires are directly reusable and are the only implementation of the `credits` refusal. The admission constants are superseded by the ladder's calibrated `tolerance`. The step-4 job is `hold vectors import --archive ID` sharing ONE path with the bundle import; keep the per-shard cursor semantics.
   **where**: app/bundles/genesis.py (1283 lines) and genesis_fetch.py

11. **does**: Ledger rows (applying/applied/rejected/evidence/tier_reached), the trust list, key loading (`SIGNATURE_BYTES`, `verify_key_from_hex`), the Lance writer lock, and T4 verify-on-use.
   **reusable**: Yes, largely as-is. `keys.py` must gain the `ed25519:` textual form (§9.2) since the header now carries it prefixed; `trust.py` must be reconciled with 09 §9.2's `state/trust.json` (`allow`/`deny`/`quarantined`) and with the rule that the allow side touches a bundle ONLY under `--strict`.
   **where**: app/bundles/ledger.py, trust.py, keys.py, writer_lock.py, verify_on_use.py

12. **does**: `vector_compliance` compares stored vectors against a fresh embedding of today's chunk text (`--stored`) or measures endpoint jitter (`--repeat N`). `vector_sweep` walks books with a stride/cluster search, re-drawing twice before condemning anything, and aborts rather than delete on the word of an unreliable encoder.
   **reusable**: Both fold into `hold vectors check [--drop]` (07 §7.6 line 428). The double-draw safeguard in vector_sweep is the most valuable thing in either file and must survive: a mismatch alone does not say which side is wrong. Note 07 §7.3 line 249 says `vectors check` "is `hold check --checks vectors-sample` over this peer's own vectors", and the migration plan fixes the sample: "`vectors-sample` draws SIXTEEN chunks per sampled shard, always; the plan sets how many shards and never how many chunks."
   **where**: tools/vector_compliance.py (246 lines), tools/vector_sweep.py (304 lines)

13. **does**: `content_hash(title, sections)`, `records_root`, `chunks_root`, `chunk_id(uid, seq, chunker_version)`.
   **reusable**: `records.content_hash` is exactly what `possession.json` must carry, and `records.chunk_id` is the id form the parquet sorts on. Use them; do not re-derive.
   **where**: app/hold/records.py:19-90


### required_behaviour

1. **detail**: Quoted: "A bundle is a directory of four files, the unit in which embedding work travels (03 §3.5; 08's signed block):" then `<bundle_id hex>/` containing `header.json` ("metadata; never text, titles or authors"), `vectors.parquet` ("chunk_id, vector[dim]; never text"), `possession.json` ("uid -> content_hash of the record the vectors were computed from") and `MANIFEST.sig` ("exactly 64 raw bytes, Ed25519"). The directory name is the bundle_id HEX — i.e. without the `sha256:` prefix. Accepted bundles live at `bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/`, which 07 §7.8 (line 665) and 09 §9.16 place under `collections/<name>/state/`, NOT in the drag: "a bundle, as the member `bundle-<space>-<bundle id>` served from `state/bundles/` (09 §9.7, §9.16)". A bundle is a member named `bundle-<space>-<bundle_id hex>` (§9.16), whose four files are four BitTorrent v2 torrents and whose member root is `H` over the canonical JSON of `[[path, root, bytes], …]` in path byte order.
   **source**: 09 §9.7 lines 762-772
   **what**: The bundle is a DIRECTORY of exactly four files, and nothing else

2. **detail**: Quoted: "Written by the producer as JSON with sorted keys, no whitespace, ASCII only (every non-ASCII character escaped), so the file is byte-stable across re-exports; verified as the bytes received. Every key below is present and no other is; strings are NFC and bounded (header at most 1 MiB, a string at most 4,096 characters, at most 10^6 uids); no floats anywhere." This differs from §9.1's canonical JSON, which mandates "non-ASCII characters unescaped" — so the bundle header is the ONE JSON object in Louise written with `ensure_ascii=True`. Likewise §9.1: "Every time in a Louise object other than a bundle header is a string, RFC 3339 in UTC to the second… The bundle header's `created_at` is an integer of Unix seconds (§9.7)." Consequence for the implementer: do not route the header through `codex.canonical_json`.
   **source**: 09 §9.7 lines 774-780
   **what**: header.json — serialisation, and it is NOT §9.1 canonical JSON

3. **detail**: | `version` | `1` | yes |
| `collection` | id | yes | the collection the producer embedded for. Provenance, never a gate: a bundle is admissible in any collection whose `space_id` it matches (03 §3.5) |
| `space_id` | id | yes | the space, and the gate: a bundle whose `space_id` is not the importing collection's is refused at T0, before any tier runs |
| `snapshot_id` | `YYYY-MM` or `YYYY-Www` | yes | the snapshot whose records were embedded. Provenance, never a gate: an unchanged record keeps its vectors across snapshots (02 §2.2), and `possession.json` — not this field — decides whether a row still matches the text |
| `shard` | string | yes | `"<slice>/<n>"` — the record shard the chunks come from; one shard per bundle |
| `uids` | list of strings | yes | the uids whose chunks the bundle covers, every chunk of each, sorted by bytes, no duplicates: the bundle's chunk range |
| `chunk_count` | integer | yes | rows in `vectors.parquet` after the challenge rows |
| `dim` | integer | yes |
| `dtype` | `"f16"` · `"f32"` | yes | the parquet element type |
| `producer_pubkey` | key | yes | the signer; `ed25519:…` |
| `encoder_image`, `encoder_weights` | id | yes | the producer's encoder build; equal to the Codex's, or a build attested compliant (03 §3.3) |
| `content_digest` | id | yes | the container-independent digest of the rows, below |
| `parquet_bytes`, `possession_bytes` | integer | yes | sizes of the two files |
| `created_at` | integer | yes | Unix seconds |
| `digest_alg` | `"sha256"` | yes | compared against the literal; never dispatched |
| `sig_alg` | `"ed25519"` | yes | likewise |
| `challenge` | object | yes | `{set: id, n: 16}` — the T2 challenge |
Note `id` means the textual form `sha256:` + 64 hex (§9.1), and `key` means `ed25519:` + 64 hex (§9.2). Sixteen keys exactly; `backend_fingerprint` is not among them. "every key below is present and no other is" — a closed schema in both directions.
   **source**: 09 §9.7 lines 782-803
   **what**: header.json — the complete field table, quoted

4. **detail**: Quoted: "Two columns, `chunk_id` (string) and `vector` (fixed-size list of `dim` elements of `dtype`); the challenge rows first in index order, then the chunk rows sorted by (uid UTF-8 bytes, `seq` as an integer) — a tuple sort, under which `:10:` follows `:2:`." So file order = 16 challenge rows (`calibration:0` … `calibration:15`) then the chunk rows. A chunk id is `<uid>:<seq>:<chunker version>` (§9.5), so `seq` is parsed out of the id, never carried as a third column. Worked example (line 861-864): "the parquet's first sixteen rows are `calibration:0` … `calibration:15`, then `gutenberg:1012:0:1`, `gutenberg:1012:1:1`, …".
   **source**: 09 §9.7 lines 805-809
   **what**: vectors.parquet — two columns, and the row order

5. **detail**: Quoted: "Parquet bytes are not stable across writers, so the signature covers the file's SHA-256 (this file, as received) and the header's `content_digest` covers the content:
```
content_digest = sha256( "louise-rag/vectors/v1" ‖ 0x00
                         ‖ u32be(dim) ‖ u32be(rows)
                         ‖ for each row in file order: u32be(len(chunk_id)) ‖ chunk_id ‖ vector as big-endian dtype bytes )
```
written `sha256:…`."
It is called container-independent because it is computed over the LOGICAL rows — dim, row count, each id and each vector's big-endian bytes — and never over parquet framing. pyarrow's `created_by` embeds the Arrow version and row-group/dictionary/statistics behaviour has moved across releases, so `sha256(parquet_bytes)` proves only "the file I received is the file that was signed" and dies on any re-encode or a non-Python writer; `content_digest` survives both and is what makes the signature meaningful to a Rust or Go implementation. Note it covers rows IN FILE ORDER, i.e. the 16 challenge rows are inside the digest even though they are outside `chunk_count`; endianness is explicit (`>f2`/`>f4`) because native-order `tobytes()` would split little- and big-endian peers.
   **source**: 09 §9.7 lines 809-819
   **what**: content_digest — what it covers and why it is container-independent

6. **detail**: Quoted in full: "A JSON object, sorted keys, ASCII, mapping each uid in `uids` to the `content_hash` of the record whose text the producer embedded. The importer compares every entry with its own record; one mismatch refuses the bundle at T0 — vectors of another revision never attach to this text. Read only after the signature verifies; its digest is inside the signed payload." `content_hash` is §9.5's BLAKE2b-128 (32 lowercase hex) of the canonical JSON of `[title, [[heading, text], …]]` — "the title and the sections exactly as the chunker sees them, so the same hash means the same chunks and the same vectors whatever the origin's own revision fields say". It is a FIELD OF THE RECORD LINE, so the importer's check is a lookup, never a fetch. Ordering constraint: the file is a separate member of the signed payload precisely so it stays out of the pre-authentication parse surface — parse it only after `verify_signature` succeeds.
   **source**: 09 §9.7 lines 821-826
   **what**: possession.json — the mapping, and when it is read

7. **detail**: ```
payload   = "louise-rag/bundle-sig/v1" ‖ 0x00
            ‖ u32be(len(header_bytes)) ‖ header_bytes
            ‖ u32be(32) ‖ sha256(vectors.parquet bytes)
            ‖ u32be(32) ‖ sha256(possession.json bytes)
MANIFEST.sig = Ed25519(producer key, payload)          -- 64 raw bytes
bundle_id    = "sha256:" ‖ hex(sha256(payload))
```
And: "The id is derived from the signed bytes, so it cannot disagree with what was signed and is not a header field; a producer that re-emits the same bundle produces the same id. `verify` proves that whoever holds `producer_pubkey` signed exactly these bytes; whether the vectors are any good is the ladder's question (03 §3.5), and whether the key may be counted is the trust file's (§9.2)." The directory name is the HEX only; the event and the ledger carry `sha256:…`.
   **source**: 09 §9.7 lines 828-843
   **what**: THE SIGNED PAYLOAD and the id — the construction, quoted

8. **detail**: §9.2: "Every signed object OTHER THAN THE BUNDLE is signed over `message = <domain> ‖ 0x00 ‖ SHA-256(canonical JSON of the body)`" and travels as `{"body": {…}, "sig": "<128 hex>"}`; "The bundle is signed over its own framed payload under the domain `louise-rag/bundle-sig/v1` (§9.7)." The differences, exactly: (1) DOMAIN NAMESPACE — `louise-rag/…` for the bundle, `louise/…` for all six envelope domains; envelope.py's DOMAINS tuple deliberately excludes it. (2) THE HEADER'S OWN BYTES ARE SIGNED, length-framed, not a digest of a re-canonicalised body — the envelope hashes `canonical_json(body)`, which means re-serialising a PARSED object, and format.py's RULE 1 (lines 23-41) documents why that is a signature-transplant primitive for attacker-supplied JSON (duplicate keys collapse, lone surrogates crash the re-encoder, NFC/NFD gives two signable encodings). (3) LENGTH FRAMING — three explicit `u32be`-prefixed parts vs one bare 32-byte digest. (4) THREE PARTS, not one: the payload binds the two sidecar files' SHA-256 as well, so a fourth part can later be added without silently changing what an old signature covers. (5) THE HEADER IS NOT CANONICAL JSON — ASCII-escaped (§9.7) where §9.1 mandates unescaped; `canonical_json` would produce different bytes for any non-ASCII title-free field. (6) BODY SHAPE — an envelope body "always carries `kind`, `version`, `key` … and `time`" as RFC 3339; the header carries `version`, `producer_pubkey` and integer `created_at`, and no `kind`. (7) THE SIGNATURE FORM AND THE ID — 64 raw bytes in a separate file vs 128 hex in a `sig` field, and `bundle_id = sha256(payload)` (of the SIGNED BYTES) vs `H(canonical JSON of body)`. `app/bundles/format.py` already owns this correctly; step 4 should keep it there and not migrate it into `app/hold/envelope.py`.
   **source**: 09 §9.2 lines 156-197 vs §9.7 lines 828-843; app/hold/envelope.py:33-45
   **what**: Why app/hold/envelope.py must NOT be used for the bundle — seven exact differences

9. **detail**: §9.7: "`challenge` | object | yes | `{set: id, n: 16}` — the T2 challenge: the first `n` rows of `vectors.parquet` are the calibration set's first `n` texts (§9.12) embedded by the producer, with `chunk_id` `\"calibration:<i>\"`, `i` from 0". n is FIXED at 16 by the schema; `set` is the calibration set's id (`H` of the 512-line jsonl file). §9.12: "T1's probe set is `vectors`; T0's envelope is `envelope`; T2's golden pairs are texts 0–15 with their rows (§9.7). … Cosines are computed in f32 after upcasting." So the importer takes rows 0..15 of the parquet, upcasts to f32, and cosines them against rows 0..15 of the `vectors` tensor in the collection's `calibration/<set>.safetensors` member. 03 §3.5, T2 row: "golden (text, vector) pairs — the calibration set's first sixteen texts and their reference vectors — re-embedded by the producer into the bundle header (09 §9.7); passes on the calibration quorum; NO AGE RULE" and "none for the importer" (cost) and "misconfiguration — proof of a correct oven, not of each loaf; not a security control". "The calibration quorum" is the Codex's `space.calibration.quorum` = `{n: 16, min_pass: 12}` (app/hold/codex.py:194-204), applied as in 03 §3.3: at least `min_pass` of the sixteen reach `space.tolerance` AND the median of them does. Structural consequences the implementer must handle: the sixteen rows are inside `content_digest` and inside `sha256(parquet)`; they are OUTSIDE `chunk_count` ("rows in `vectors.parquet` after the challenge rows"); they carry no uid and so appear in neither `uids` nor `possession.json`; and T0's rule "for every record, the bundle's set of chunk ids equals the set `chunks` derives locally" must be applied only to rows 16..end.
   **source**: 09 §9.7 line 803; §9.12 lines 1195-1200; 03 §3.5 T2 row
   **what**: THE CHALLENGE ROWS: n = 16, chunk_id `calibration:<i>`, and what T2 does

10. **detail**: This branch ships the SET (the 512 texts, `calibration/text.jsonl`, id frozen at `sha256:6a1bac6e…`, `app/hold/calibration.py`) but NOT the MEMBER — the `calibration/<set>.safetensors` holding `vectors [512, dim] f32` and `envelope [2, dim] f32`, which §9.12 says is "written by `hold collection create` (06 §6.6) and a member of every snapshot". `app/hold/drag.py:174` gives its path and `app/louise.py:271-278` READS it, raising "no calibration member … It is written by `hold collection create`" — but nothing in the tree writes it (`grep safetensors` finds only readers, and `safetensors` is not in requirements.txt while `app/louise.py:300` imports it). So: T2 CANNOT RUN AT ALL until the member writer lands, and step 4 depends on a step-3 gap. Two further gaps in the reader: `app/louise.py:298-303` does not verify `space.calibration.vectors` = `H` over the `vectors` tensor bytes nor `space.envelope` = `H` over the `envelope` tensor bytes, which §9.12 requires ("a member whose tensors hash otherwise is refused") and which is the ONLY thing binding the reference vectors to the Codex; and the producer side needs the same 16 reference texts to embed at build time. Note the producer embeds texts 0–15 with its own encoder — a fixed, deterministic, publicly known set, so it may be computed ONCE per (encoder build, set) and cached for the life of the process; nothing in §9.7 requires a fresh embed per bundle, and 03 §3.5's "no age rule" removes the only reason it might.
   **source**: 09 §9.12; app/hold/calibration.py; app/louise.py:271-303; app/hold/drag.py:109,174-176
   **what**: How the challenge interacts with the calibration member this branch ships

11. **detail**: T0 is `space_id` equality ("refused at T0, before any tier runs", exit 1 — the bundle is the failing unit) plus: "dimension compared four ways — bundle header, vector column width, the Codex's `space.dim`, the local table — and dtype match the space; unit norm; no NaN, no duplicates; for every record, the bundle's set of chunk ids equals the set `chunks` derives locally, or the record is refused whole; the bundle's mean vector has cosine ≥ 0.9 to the envelope's mean and every per-dimension standard deviation lies within half and twice the envelope's". `collection` and `snapshot_id` gate NOTHING. One extra refusal is on the ARCHIVE: look `producer_pubkey` up in the platform registry; if it names an archive, refuse unless the Codex's `credits` names it. `--strict` reads the trust ALLOW side (the only place it touches a bundle) and "never admits below T3".
   **source**: 03 §3.5; 07 §7.3 lines 234-248
   **what**: Import gates, in the order the ladder fixes

12. **detail**: An accepted bundle's rows are merged into `vectors/<space>/<slice>/<NNNNN>.safetensors`: tensors `vectors [n, dim]` in `space.dtype` ("one row per chunk of record shard `n`, in shard order: records in order, each record's chunks by `seq`; a row without an accepted vector is all zeros") and `labels [n] u32` (`0xFFFFFFFF` for a row with no vector); headers `chunk_ids_sha256` (`H(id_0 ‖ id_1 ‖ …)` over the chunk ids in row order), `producers` ("the canonical JSON array of the bundle ids merged, sorted"), `index_version`, `index_min`, `collection_id`, `snapshot_id`, `space_id`, `shard`. "The row for a chunk is the vector from the first bundle accepted for it (04 §4.5); a later bundle for the same chunk is compared and not stored, unless the first is flagged. Labels are computed by the importer against the centres it holds, never read from a bundle." These files are LOCAL only: "never a member's bytes, never fetched as such — vectors travel only as bundles (04 §4.3)".
   **source**: 09 §9.6 lines 688-706
   **what**: At rest, after acceptance: the per-shard safetensors of §9.6

13. **detail**: "Record shards, text-index members and bundles are members; vectors reach a peer only as bundles (03 §3.5, 09 §9.7). A request for a `vectors-<space>-<slice>` member or a bucket group resolves to the accepted bundles the sharer holds for those chunks, delivered as bundles with their signatures, and the fetcher merges them itself (02 §2.4)." Crucially, a bundle is NOT promoted by the snapshot-root rule: "The members no snapshot lists are admitted by their own rule instead, never by this one: a bundle by the ladder of 03 §3.5…" Every byte draws from the one token-bucket budget, and bundles ride the *archive* class ("pre-embedded vector archives, useless without the corpus and so always behind it") or *delta*/*backfill* as appropriate. The announcing event (§9.9) names `{collection, space_id, snapshot_id, shard, uids: [first, last], bundle_id, root, bytes}` — note `uids` collapses to a RANGE in the event while the header lists them all.
   **source**: 04 §4.3 lines 160-190
   **what**: How a bundle travels


### size_risks

1. 

2. 

3. 

4. 

5. 

6. 

7. 

8. 


### unspecified

1. 

2. 

3. 

4. 

5. 

6. 

7. 

8. 

9. 

10. 

11. 



## ladder â the T0-T3 admission ladder, and what each tier costs

ADMISSION (docs/03 §3.5, §3.3, 04 §4.5, 09 §9.7/§9.10/§9.12). Two bindings decide admissibility — the bundle's `space_id` and, per record, `content_hash`; `collection_id` and `snapshot_id` are provenance and gate nothing, and no producer key need be known, trusted or seen before. A third refusal, about attribution not correctness, resolves `producer_pubkey` through the platform registry to an archive id and refuses when the Codex's `credits` does not name it. Then a five-rung ladder T0–T4, cheapest first, each gating the next; a bundle is usable once T0, T1 and T2 pass, T3 deferred is recorded as skipped and re-run later by `hold vectors check`, and T4 runs on chunks a search actually cites.

The code that exists (app/verify/, app/bundles/) is a DIFFERENT ladder, built from architecture-v2.md §6 before the specs were written. It is not a wrap-and-adapt job: every tier disagrees with 03 §3.5 in substance, not in wording. The seven that matter most: (1) T3 is a truncated Wald SPRT over up to 64 chunks against a constant COARSE_TAU=0.95, not sixteen chunks on the calibration quorum against the Codex's `tolerance` (app/verify/t3.py:39-43, 94-118, 148-185, 203-233) — the step-3 study's report is confirmed exactly. (2) `app/bundles/trust.py:72-101` implements trust-on-first-use and REFUSES every producer key after the first, which 03 §3.5 and 08 §8.1 forbid in as many words. (3) `possession.json` carries `raw_hash` (the origin bytes' BLAKE2b-128, i.e. 09 §9.5's `origin_hash`) where 09 §9.7 requires `content_hash` (BLAKE2b-128 of canonical JSON of `[title, sections]`) — a substantive change, because `content_hash` is on the record line and `origin_hash` requires re-reading the origin. (4) T2 is a beacon-and-age scheme with an artifact-published golden bank, where the spec's T2 is the calibration set's first sixteen texts embedded into the first sixteen rows of `vectors.parquet` with "no age rule"; and `format.py:278-281` currently REFUSES any non-null `challenge`, so no spec-shaped bundle can be parsed at all. (5) T0's envelope test is a bootstrapped D-statistic from a signed "space artifact" that does not exist for any collection here; the spec's is two flat comparisons against the calibration member's `envelope` tensor. (6) The whole calibrated ladder is skipped when no artifact is found (app/bundles/importer.py:224-248), so today's real admission is signature + TOFU + `space_id` + content digest + raw-hash possession + local chunk-set equality + `checks.py`'s coarse gates at a hardcoded 0.95. (7) The archive refusal's two halves exist (`app/hold/template.py:78-101`) and NOTHING calls them.

Nothing writes the calibration member `calibration/<set>.safetensors`, which T0's envelope, T1's probes, T2's goldens and the gate's reference all read. That is step 4's first blocker.

SIZE. PubMed is 36M records, one chunk each under the `abstract` layer, red-class. Its layer is not `fixed`, so T1 never runs on it; a peer that has not run `hold records fetch` has no text, so T3 never runs either — on this project's largest collection admission rests on T0 and T2 alone, and T2 is explicitly "not a security control". Several code paths scale with the corpus rather than the bundle: `chunk_verification` has no index on `bundle_id` while `resweep` selects on it, `verify_chunks` builds one SQL placeholder per chunk id against a 10^6-uid cap, and `codex.sample()` sorts its universe twice, which the centres-training rule applies to all 36M chunk ids.

### disagreements

1. **problem**: CONFIRMED, and it is the biggest one. The step-3 study's report is exactly right. T3 is a truncated Wald SPRT on miss indicators, not the calibration quorum. Concretely: (a) the sample size is `min(n_max, len(available))` = up to 64 chunks (:203, :207), and `DEFAULT_N = 16` (:39) is used ONLY as the stats label `"n_target": n` (:237) — it never affects anything, so the module does not even do its own docstring's "n~16"; (b) the decision is `sprt.decide(misses, observed)` crossing `A = ln((1-beta)/alpha)` or `B = ln(beta/(1-alpha))` (:109-118), with a truncation rule `"fail" if value > 0 else "pass"` — there is no min_pass count and no median anywhere in the file; (c) tau comes from a space artifact's calibration table or falls back to `COARSE_TAU = 0.95` (:43, :164-165), and for an untrusted artifact is clamped to `max(tau, COARSE_TAU)` (:179) — a constant threshold, which 03 §3.1 forbids in as many words ("No number in this paragraph is a threshold: `tolerance` comes from calibration and from nowhere else"); (d) `codex.space.tolerance` is never read by this file or anything it calls; (e) up to 64 embeds against a 10,000-chunk bundle is 0.64% of production, not the spec's "about a fifth of a percent".
   **suggestion**: Replace `check_spot_recompute`'s decision with the quorum. Draw exactly `codex.space.calibration.n` (16) chunk ids with `secrets.SystemRandom().sample` from the chunks whose text this peer holds; embed them in one call; compute cosines with `app.hold.embedder.cosine`; fail iff `passed < min_pass or median < tolerance` — i.e. call `Run.ok`'s predicate. Keep `choose_sample`'s OS-entropy default and the test that asserts it. Keep `detection_probability`/`hypergeometric_detection` as reported EVIDENCE (they are honest and useful in the verdict payload) but delete `Sprt`, `DEFAULT_N_MAX`, `DEFAULT_ALPHA`, `DEFAULT_BETA` and `COARSE_TAU` from the decision path. If the SPRT is worth keeping at all it belongs in a note about why the spec chose a fixed-n quorum instead — the spec's own reason is in 09 §9.3: a plan-settable n "would need a scaling rule for the threshold that a creator could get backwards".
   **where**: app/verify/t3.py:14-30 (docstring), :39-43 (constants), :94-118 (`Sprt`), :203-207, :224-233 (`check_spot_recompute`)

2. **problem**: TRUST-ON-FIRST-USE IS AN ADMISSION RULE, AND THE SPEC FORBIDS ONE. The first producer key a collection sees is trusted and recorded; every later key raises `BundleUntrusted` ("is not trusted by this node, which already trusts N other key(s)"). 03 §3.5 is explicit in the opposite direction: "Admission is the ladder below and nothing else: no producer key needs to be known, trusted or seen before; the trust list (07 §7.3 `hold key trust`) filters attestations (§3.6) and lifts burns, and its ALLOW side imposes nothing on a bundle — an empty allow list, which is what an honest newcomer has (08 §8.1), admits every producer in existence." And 04 §4.5: "a key is refused only after this peer has denied it outright or a proof of forgery has burned it ... never for being absent from an allow list." 08 §8.1 names precisely this as one of the two defences "rejected outright": a newcomer's empty list must not verify nothing forever. This is not a wording drift — on the second peer to send a bundle, today's node refuses correct work.
   **suggestion**: Reduce `check_admission` to two refusals: the key is explicitly DENIED (a mute, 09 §9.2) or the key is QUARANTINED by a proof of forgery. Delete the TOFU branch and the `existing = trusted_keys(con)` refusal. Move the allow-list consultation behind `--strict` only, per 07 §7.3, where it "refuses a bundle whose producer is not on this peer's allow list and never admits below T3 ... with an empty allow list ... it admits nothing at all, which is why it is off by default". Keep `quarantine_key` and its `--restore` path (03 §3.5: "permanent until an operator runs `hold key trust <key> --restore`").
   **where**: app/bundles/trust.py:72-101 `check_admission`; app/bundles/importer.py:357-359

3. **problem**: SPEC vs SPEC, on the granularity of the content_hash refusal. 09 §9.7: "The importer compares every entry with its own record; one mismatch refuses the BUNDLE at T0 — vectors of another revision never attach to this text." 03 §3.5: "a record whose `content_hash` in the bundle differs from the one in this peer's copy has ITS ROWS refused — the record was re-issued since — with the refusal recorded, never counted as a failure." 07 §7.3 sides with 03: "has that record's rows refused and the refusal recorded ... and the import exits 0, never a failure." Two of three say per-record and exit 0; one says whole-bundle at T0. The behavioural difference is large at PubMed scale: a bundle covering 10^6 uids where one abstract was revised is either fully imported minus one record, or fully rejected.
   **suggestion**: Ask the operator; my reading is that 09 §9.7 is the stale sentence — 03 §3.5 and 07 §7.3 agree with each other and with the design rationale (a revision is normal traffic on a living corpus, not a fault). Implement per-record refusal, record it, exit 0, and raise a spec amendment against 09 §9.7's sentence.
   **where**: 09 §9.7 (`possession.json` paragraph) vs 03 §3.5 and 07 §7.3

4. **problem**: THE WRONG HASH. `possession.json` is built and verified as uid → `raw_hash`, "the blake2b(digest_size=16) hash of a document's RAW BYTES" obtained from `plugin.fetch` (possession.py:49-79). 09 §9.7 requires uid → the record's `content_hash`, which 09 §9.5 defines as "BLAKE2b-128 of the canonical JSON of the array `[title, [[heading, text], …]]` — the title and the sections exactly as the chunker sees them, so the same hash means the same chunks and the same vectors whatever the origin's own revision fields say." Both are BLAKE2b-128 so the wire width matches and nothing would visibly break; the preimages are unrelated. What the current hash actually equals is 09 §9.5's `origin_hash`. Three consequences: (a) the binding no longer says what the spec wants it to say — two peers that parsed the same origin bytes with the same toolchain get the same `content_hash` and can share vectors, but a re-compressed or re-served origin file breaks `raw_hash` for no reason; (b) verifying costs a document read from the plugin per uid instead of a lookup on the record line; (c) on a red-class collection the pointer record CARRIES `content_hash` ("every other field, `content_hash` and `origin_hash` included, is that of the full record") while the origin bytes are an XML container member — so `content_hash` is checkable by a textless peer and `raw_hash` is not.
   **suggestion**: Change both sides to `content_hash`, read from the record line (or recomputed with `app.hold.records.content_hash(title, sections)` where the text is held). Keep possession.json as a separate file read only after the signature verifies — that part of the design is right and 09 §9.7 endorses it ("Read only after the signature verifies; its digest is inside the signed payload").
   **where**: app/bundles/possession.py (whole file); app/bundles/format.py:315-338 `parse_possession`; app/bundles/importer.py:376-379

5. **problem**: T2 IS A DIFFERENT MECHANISM, AND THE PARSER CANNOT ACCEPT THE SPEC'S. The code derives challenge indices from a beacon keyed on `created_at` (t2.py:51-61, 96-100), reads challenge vectors from a `header.challenge` object (:102-113), compares them against an artifact's golden bank with a worst-of-tau rule (:117-143), and enforces `max_bundle_age_seconds` (default 30 days) and `max_clock_skew_seconds` (:83-94). The spec's T2: the calibration set's FIRST SIXTEEN texts, their reference vectors from the calibration member, embedded by the producer into the FIRST SIXTEEN ROWS OF vectors.parquet with chunk_id `"calibration:<i>"`, header `challenge = {set: id, n: 16}`, passing "on the calibration quorum" and with "no age rule" written explicitly in the table. Separately, `format.py:278-281` REFUSES any bundle whose `challenge` is non-null, so a spec-shaped bundle cannot even be parsed today.
   **suggestion**: Rewrite t2 as: read `header.challenge = {set, n}`; refuse when `set != codex.space.calibration.set`; take the first n rows of vectors.parquet, assert their chunk_ids are `calibration:0` … `calibration:n-1` in order; cosine each against the calibration member's `vectors[i]`; pass on the quorum. Delete the beacon, the golden bank, and both age rules. Note `chunk_count` counts rows AFTER the challenge rows, so the parquet row count is `chunk_count + challenge.n` — a check the current `_read_parquet` (importer.py:146-150) gets wrong once challenges exist.
   **where**: app/verify/t2.py (whole file); app/bundles/format.py:275-281

6. **problem**: THE ENVELOPE TEST IS A DIFFERENT STATISTIC, AND IT NEEDS AN ARTIFACT THAT DOES NOT EXIST. The code computes `D = sum_j z_j^2` and compares it against an artifact's bootstrapped null quantile per (n_docs, n_chunks) bucket, plus a stratified pairwise-cosine band. The spec's T0 envelope clause is two flat comparisons against the calibration member's `envelope [2, dim]` tensor: "the bundle's mean vector has cosine >= 0.9 to the envelope's mean and every per-dimension standard deviation lies within half and twice the envelope's". No bootstrap, no buckets, no alpha budget, no artifact — the calibration member is a member of every snapshot and every holder has it. The code's version SKIPS entirely when there is no artifact (:91), which is every collection on this node.
   **suggestion**: Implement the spec's two comparisons against `envelope[0]` and `envelope[1]`. The 0.9 / 0.5 / 2.0 numbers are literals the spec writes out, unlike `tolerance` — so no calibration is needed and none should be invented. Keep the D-statistic and the stratified pair cosines as recorded EVIDENCE if you want them, never as decisions, and note in the module docstring that the multiple-comparison analysis at t0.py:7-25 is genuinely good work that the spec's simpler rule sidesteps.
   **where**: app/verify/t0.py:88-135 `check_moment_envelope`, :163-212 `check_pair_cosines`

7. **problem**: Two mismatches. (a) The probe profile is a GATE — `check_probe_profile` returns FAILED when `D_P > critical` (:176-181). 03 §3.5 says the profile is "recorded as evidence", full stop; the only T1 decision the spec names besides the band is the realisability triple. (b) The overlap band's reference comes from an artifact's published bins keyed on measured phi (:208-211, :286-290). The spec says the band is "measured on the local text — the 1st to 99th percentile of the same statistic OVER THE VECTORS THIS PEER ALREADY HOLDS for that layer, not run until it holds a thousand such pairs". That is self-measured local state, not a published number, and the thousand-pair floor is nowhere in the code (only `MIN_OVERLAP_PAIRS = 8` at :40, which is the spec's OTHER floor — "when the bundle has fewer than eight adjacent pairs"). (c) The layer-kind skip is absent: nothing consults whether the layer is `fixed`. It happens to work out for PubMed because `chunk_abstract` emits one chunk per document (the comment at :231-234 says so), but a `cdc` or `verbatim` layer WOULD produce adjacent pairs and would be tested against a band the spec says "does not exist and is not run".
   **suggestion**: Demote the probe profile to evidence. Key the skip on `codex.layers[i].chunker.id == "fixed"` explicitly, per layer, not on how many pairs happen to be found. Compute the band from this peer's own held vectors for that layer, maintained as running per-layer state, and skip until a thousand pairs exist. Keep `check_gram_gate` and its honest scope note at t1.py:8-33 — it is the spec's realisability triple and the docstring's admission that it is an integrity canary rather than anti-forgery is exactly right.
   **where**: app/verify/t1.py:117-184 `check_probe_profile`; :187-264 `check_overlap_band`

8. **problem**: THE CALIBRATED LADDER IS UNREACHABLE ON THIS NODE. It runs only when `find_space_artifact(header.space_id)` finds a signed "space artifact" under `SPACE_ARTIFACT_DIR` or `CONFIG_DIR/spaces` — an object no `hold` command produces and no collection here has. When it is absent the function returns None (:238) and admission falls back to `checks.py`'s coarse gates with `MODEL_IDENTITY_FLOOR = 0.95` on 8 chunks. So the ACTUAL admission ladder in production is: signature + TOFU + space_id + content digest + raw-hash possession + local chunk-set equality + isotropy + overlap-vs-baseline + degenerate + worst-of-8 at 0.95. That is not T0–T3, and `tier_reached` is NULL for every such import.
   **suggestion**: The space artifact concept has no place in the specs — its four jobs (envelope, probes, goldens, tau) all belong to the calibration member (09 §9.12: "Three uses, one file ... T1's probe set is `vectors`; T0's envelope is `envelope`; T2's golden pairs are texts 0–15 with their rows") and to `space.tolerance`. Retire `app/verify/artifact.py` and the `[trust] space_authors` plumbing along with it, and make the member the single source. That also deletes the whole tighten-only-for-untrusted-artifacts apparatus (t2.py:166-170, t3.py:152-184), which exists only because an artifact is an untrusted third object; the calibration member is bound by the Codex's hashes and needs no such rule.
   **where**: app/bundles/importer.py:224-248 `_run_ladder_if_possible`; app/bundles/importer.py:196-221

9. **problem**: Refuses the WHOLE BUNDLE when any one uid's chunk-id set differs from the locally derived one (`raise BundleFormatError`). 03 §3.5's T0 row says "for every record, the bundle's set of chunk ids equals the set `chunks` derives locally, OR THE RECORD IS REFUSED WHOLE" — the record, not the bundle. Same granularity error as the possession one, and at PubMed scale it turns one re-chunked document into the loss of a million-record bundle.
   **suggestion**: Refuse per record, record the refusal, continue. Also route the local derivation through `app/layers.py:chunks_for_document` (it already does, at importer.py:166-170 — good) and derive the layer from the Codex's rules rather than from anything the bundle says, per 03 §3.2's `chunks` row.
   **where**: app/bundles/importer.py:381-398 (step 6, local chunk agreement)

10. **problem**: KEY FORM MISMATCH, and the refusal is not wired. `archive_of_key` matches `producer_pubkey` against `GenesisArchive.keys`, whose test fixture uses `"ed25519:" + "ff"*32` (tests/hold/test_template.py:332) — 09 §9.2's key text form, which 09 §9.7 also specifies for the header field ("`producer_pubkey` | key | yes | the signer; `ed25519:…`"). But `parse_header` validates it as bare 64-hex (`_check_hex(obj["producer_pubkey"], "producer_pubkey", 32)`). So even once wired, a lookup would never hit. And nothing calls `credits_admit` at all — grep finds only the tests.
   **suggestion**: Move the header field to the `ed25519:…` form as part of the v1-shape fix, then call `template.credits_admit(codex, header.producer_pubkey)` in `_import_checked` between the space check and the ladder, refusing with a distinct error class recorded in the ledger. For `hold vectors import --archive ID` the key lookup is unnecessary — check the id against `codex.credits` directly, per 06 §6.6.
   **where**: app/hold/template.py:78-91 `archive_of_key` vs app/bundles/format.py:306

11. **problem**: SPEC vs SPEC on what `tiers.T1` records. §9.10 says "T1 is skipped on a non-`fixed` layer" and its PubMed example writes `"T1":"skipped"` with the footnote "T1 is skipped because PubMed's layer is `abstract`, not `fixed`." But §3.5's T1 has three parts and only the third is layer-conditional: the cosine profile is recorded on any layer, and the realisability triples "must be geometrically realisable" on any layer. So on an `abstract` layer the realisability gate still runs and can still fail — yet the map has no way to say "T1 ran partly". §3.5's own third skip condition for the band ("not run until it holds a thousand such pairs") is also absent from §9.10.
   **suggestion**: Flag to the operator. My reading: §9.10's `tiers` is a coarse summary and "skipped" there should mean "no T1 DECISION was reached" — but as written an implementer will plausibly skip the realisability gate on PubMed, which is the one T1 part that costs nothing and catches mis-decoded bytes. Ask for one sentence in §9.10 saying the realisability gate runs on every layer and `T1` records `"pass"` when it alone ran.
   **where**: 09 §9.10 (`tiers` field and worked example) vs 03 §3.5's T1 row

12. **problem**: Three statements about the same threshold that are consistent only if read carefully, and an implementer will get it wrong. §3.5: usable at T0+T1+T2. §4.5: "A coverage claim counts — for the cascade, for adoption, for §4.7 — only when the bundle it names has been fetched and passed T0–T3." §4.4, for a textless red peer: "a peer holding vectors without text runs only T0 and T2, may serve what it holds, and does not attest." So there are three distinct states — usable, claimable, attestable — and only §4.4 says the third out loud. Compounding it: a textless peer cannot run all of T0 either, because T0's chunk-geometry clause ("the bundle's set of chunk ids equals the set `chunks` derives locally") needs the text, and 04 §4.4 itself lists `chunks` among the checks that "run where the text is".
   **suggestion**: State the three states explicitly in the implementation and in the coverage map: `accepted_at` non-null (usable) needs T0,T1,T2 not-failed with T0's decidable parts decided; a counted coverage claim needs T3 passed; an attestation needs T3 passed on an encoder with a current calibration statement. And ask the operator what T0 means on a textless red peer — my reading is that the chunk-geometry clause is skipped there and only the algebra and the envelope run.
   **where**: 03 §3.5 "A bundle is usable once T0, T1 and T2 pass" vs 04 §4.5 "passed T0–T3" vs 04 §4.4 "runs only T0 and T2"

13. **problem**: `backend_gate.TAU` is a module constant that `vector_compliance` uses as its default pass bar (`--tau` default `backend_gate.TAU`). Migration plan step 3 already recorded that backend_gate "records its own first run as a baseline and PASSES" and "thresholds on a constant TAU = 0.995 — which 03 §3.1 forbids in as many words", and that it "is replaced, not wrapped". Step 4 inherits the dependency: `hold vectors check` must take its bar from `codex.space.tolerance`.
   **suggestion**: When `hold vectors check` wraps `vector_compliance`, drop the `backend_gate` import entirely and resolve tolerance from the Codex. The `--repeat`/`--concurrency` mode is worth keeping as `hold embedder ...` diagnostics — its note that "a reproducibility number without a stated concurrency is not a number" is hard-won and should not be lost.
   **where**: app/verify/backend_gate.py; tools/vector_compliance.py:222-226

14. **problem**: T4 burns a producer on a SINGLE miss. 03 §3.5 requires "a T4 miss that REPRODUCES ON A SECOND RECOMPUTE, on a peer whose encoder holds a current calibration statement for the space" before it is a proof of forgery. The current code calls `set_verified(..., passed=False)` and quarantines immediately, with no second recompute and no check that this peer's encoder is currently compliant. Given that the encoder is known to be non-deterministic on this node (tools/vector_compliance.py's whole docstring: intermittent 0.78–0.81 excursions on long inputs on the Arc build), a single-miss burn will quarantine honest peers.
   **suggestion**: Add the second recompute and the calibration-statement precondition before any burn, and record the two cosines in the forgery attestation's evidence. This is the one place where the existing code is not merely differently-shaped but actively unsafe against a known property of this node's encoder.
   **where**: app/bundles/verify_on_use.py:118-131


### existing_code

1. **does**: The calibration gate's pass predicate, which IS the quorum T3 must reuse: `Run.ok` at :77-82 returns `self.passed >= self.min_pass and self.median >= self.tolerance`, with `passed` counting cosines >= tolerance (:65-67), `median` = `statistics.median` over the drawn n (:73-75). `cosine` at :97-107 implements 03 §3.1's f32 rule exactly (upcast both, renormalise, dot).
   **reusable**: Directly. T3 should build a `Run`-shaped result over sixteen CHUNK cosines and reuse `Run.ok`'s predicate verbatim, or lift the predicate into a shared `quorum_pass(cosines, tolerance, min_pass)`. `min_pass` and `n` come from `codex.space.calibration` (app/hold/codex.py:195-203, defaults n=16 min_pass=12, validated `0 < min_pass <= n` at codex.py:600-601); `tolerance` from `codex.space.tolerance`, a decimal string validated at codex.py:598-599.
   **where**: app/hold/embedder.py:48-107 (`Run`, `Run.ok`, `cosine`)

2. **does**: 09 §9.1's one seeded sampler (HMAC-SHA256, first 8 bytes big-endian, lowest n, ties by id bytes) and the fraction→count rule `max(1, ceil(f*N))`.
   **reusable**: For `vectors-sample` (published seed) and for the gate's draw of quorum.n texts from the 512. NOT for T3, which must use `secrets.SystemRandom` — the spec forbids any bundle- or content-derived seed there. See size_risks for the double sort.
   **where**: app/hold/codex.py:1029-1053 `sample()`, :1056-1066 `sample_size()`

3. **does**: 09 §9.5's hashes, already correct.
   **reusable**: `content_hash` is exactly the value `possession.json` must carry and compare. `chunk_id(uid, seq, chunker_version)` is the id form T0's chunk-geometry clause must derive locally through `app/layers.py:chunks_for_document` (CLAUDE.md invariant 1: never a plugin's chunker directly).
   **where**: app/hold/records.py:30 `content_hash(title, sections)`, :46 `records_root`, :68/:216 `chunks_root`, :85 `chunk_id`

4. **does**: 03 §3.5's archive refusal, both steps, already written and tested (tests/hold/test_template.py:332-356). `archive_of_key` scans `app.bundles.genesis.ARCHIVES` for an entry whose `.keys` tuple contains the pubkey; `credits_admit` returns True when the key belongs to no archive, else True only if `codex.credits` names that archive.
   **reusable**: As is — but NOTHING calls it. Wiring it into the import path is step 4's work. See disagreements for the key-form mismatch.
   **where**: app/hold/template.py:78-91 `archive_of_key`, :95-101 `credits_admit`

5. **does**: The platform registry: `archive_id`, `base_url`, adapter, shards, `uid_prefix`, `ids_per_shard`, and the four credit fields (`producer`, `license`, `attribution`, coverage via `coverage()`), plus `keys: tuple[str, ...]` at :135 — the set 03 §3.5 resolves a `producer_pubkey` through. `credit()` at :137-144 emits 09 §9.3's credit object with the keys deliberately excluded.
   **reusable**: Yes; this is already post-ad4c352 shaped. Both current entries have `keys=()` (MedCPT is a directory of .npy on FTP, Upstash ships parquet) so the refusal correctly never fires for them today.
   **where**: app/bundles/genesis.py:85-165 `GenesisArchive`, :175-216 `ARCHIVES`/`DEFAULT_ARCHIVE_BY_PLUGIN`

6. **does**: Wire-format hygiene that is genuinely excellent and should survive verbatim: hash the received bytes, never re-serialise (:24-41); duplicate-key rejection (:134-138); float/NaN/Infinity rejection (:125-131); lone-surrogate and NFC checks (:141-168); bool-is-not-int (:171-177); closed schema both directions (:257-265); pinned `digest_alg`/`sig_alg` literals never dispatched (:269-273); caps applied before allocation (:92-97); `content_digest` big-endian with the (uid bytes, int seq) tuple sort (:399-440).
   **reusable**: Keep the machinery; change the field list. `_HEADER_KEYS` (:102-107) must gain `snapshot_id`, `shard`, `encoder_image`, `encoder_weights` and drop `backend_fingerprint`; `challenge` must become `{set, n}` instead of the refused-if-non-null slot at :278-281; `producer_pubkey` must move from bare 64-hex (:306) to 09 §9.2's `ed25519:…` text; `space_id` is validated as 16 hex (:300) where 09 §9.3's space_id is a full `sha256:…` and the 16-hex form is only the PATH segment.
   **where**: app/bundles/format.py (whole file)

7. **does**: The gate order — signature → header → trust → space → content digest → possession → local chunk agreement → ladder + coarse checks → apply — and the crash-safety ordering at :450-469 (clear `embed_done` BEFORE Lance's delete-then-add, mark done after), plus ledger `applying`→`applied`.
   **reusable**: The ORDER and the crash-safety pattern are right and match CLAUDE.md invariant 2. Steps 2 (trust) must go; step 5 (possession) must change hash; step 6 must refuse the record not the bundle; step 7 must become the real ladder. Also `import_bundle` at :301-330 never raises to the caller, which matches "a peer's bundle never crashes the worker".
   **where**: app/bundles/importer.py:333-490 `_import_checked`

8. **does**: `tier_reached` = highest tier that RAN and passed (:305-309); `passed` is False when nothing ran (:299-303, "an all-skipped ladder is not a pass"); full untruncated evidence (:318-335).
   **reusable**: The three properties are exactly 03 §3.5's "A tier that could not run is recorded as not run, never as passed". But the shape must become 09 §9.10's per-tier `{T0,T1,T2,T3}` map of pass/fail/skipped, not a single high-water integer.
   **where**: app/verify/verdict.py:283-340 `Verdict`

9. **does**: T4 in the right shape: `pending()` at :76-84 (rows exist only for imported chunks, absence means locally embedded), permanent per-chunk verified state, quarantine on failure plus re-queue of the producer's other documents (:135-150), `annotate()` at :204-222 for the per-hit badge, `resweep()` at :168-201 for re-running when the endpoint returns.
   **reusable**: The design matches §3.5 closely. Missing: the "reproduces on a second recompute" requirement before burning (:123 burns on one miss), the forgery attestation (09 §9.8), the red-class substitution of `content_hash` for `text`, keying the mark on the ENCODER as well as the chunk id, and re-running when the TEXT arrives rather than only the encoder.
   **where**: app/bundles/verify_on_use.py

10. **does**: `bundles` table (bundle_id PK, producer, space_id, state, uids JSON, signature, digests, `space_weak`, `tier_reached`, verdict), `bundle_evidence`, `chunk_verification`, `providers`, `genesis_imports`.
   **reusable**: Close to 09 §9.10's coverage map but missing its columns: `shard` as the rendezvous `"<collection_id>/<space>/<slice>/<n>"`, `uids` as `[first, last]`, `tiers` as the four-way object, `accepted_at`, `source` (`local`/`fetched`/`archive`), `verified_on_use`. `genesis_imports` is where `source: "archive"` comes from.
   **where**: app/bundles/ledger.py; app/db.py:112-188

11. **does**: Resolves a collection, locates `drag/calibration/<hex>.safetensors`, loads its `vectors` tensor, builds the embed recipe from the Codex (dtype/ctx/dim from the Codex, URL from EMBED_URL only for now), and wraps every encoder failure as `GateDeferred`.
   **reusable**: The same member load is what T0 (envelope), T1 (probes) and T2 (goldens) need — extend `_read_reference` to return the `envelope` tensor too, and add the missing check that the tensors hash to `space.calibration.vectors` and `space.envelope` (09 §9.12: "a member whose tensors hash otherwise is refused"). Note the member is never WRITTEN by `hold collection create` today (nothing in app/hold/ produces it) — that is a hard blocker for every tier.
   **where**: app/louise.py:263-330 `_gate`, `_read_reference`, `_recipe_for`

12. **does**: Artifact-free coarse gates: structural, isotropy against the uniform-sphere null, adjacent-chunk overlap against the bundle's own random-pair baseline, degenerate-duplicate, and 8-chunk sample re-embed at a hardcoded 0.95.
   **reusable**: Mostly superseded. `check_structural` folds into T0. The isotropy and degenerate tests are cheap and catch things the spec's T0 does not name explicitly — keep them as extra evidence, never as the spec's decisions. `check_sample_reembed`'s `MODEL_IDENTITY_FLOOR = 0.95` (:71) must go: 03 §3.1 forbids a constant threshold.
   **where**: app/bundles/checks.py

13. **does**: `check_stored` compares stored vectors against a fresh embedding of the chunk text as chunked today, reporting chunk-id overlap so a re-chunk is distinguishable from drift; `--repeat N` measures endpoint self-consistency at a stated concurrency.
   **reusable**: These two are what `hold vectors check [--drop]` wraps (migration plan step 4). The right reading of the spec is that `hold vectors check` is `hold check --checks vectors-sample` over this peer's own vectors (07 §7.3), i.e. sixteen chunks per sampled shard on the quorum — so `--books/--chunks` becomes shard-fraction-and-sixteen, and `--tau` must come from `space.tolerance` rather than `backend_gate.TAU`.
   **where**: tools/vector_compliance.py; app/bundles/verify_on_use.resweep


### required_behaviour

1. **detail**: Quoted whole: "dimension compared four ways — bundle header, vector column width, the Codex's `space.dim`, the local table — and dtype match the space; unit norm; no NaN, no duplicates; for every record, the bundle's set of chunk ids equals the set `chunks` derives locally, or the record is refused whole; the bundle's mean vector has cosine >= 0.9 to the envelope's mean and every per-dimension standard deviation lies within half and twice the envelope's (§3.3)". NEEDS: the copy only (no encoder, no artifact) plus the Codex and the calibration member's `envelope` tensor. COSTS: "tens of ms, CPU". CATCHES: "missing normalisation, random or foreign vectors, a wrong chunk geometry; sanity only, never a security control". FAILING: 07 §7.3 — `space_id` mismatch is refused "at T0 (exit 1 — the bundle is the failing unit)". Note the two granularities inside T0: a chunk-geometry mismatch refuses THE RECORD whole; a space mismatch refuses the bundle. Also note 0.9, 0.5 and 2.0 ARE fixed numbers here — unlike `tolerance`, the spec writes them as literals.
   **source**: 03 §3.5 table; 09 §9.7
   **what**: T0 algebraic — what it checks

2. **detail**: Quoted whole: "each vector's cosine profile against the calibration vectors, recorded as evidence; sampled (vector, probe, probe) triples must be geometrically realisable, which real vectors cannot fail and mis-decoded bytes do; and, for `fixed` layers, consecutive chunks overlap by construction, so their cosine must sit in a band measured on the local text — the 1st to 99th percentile of the same statistic over the vectors this peer already holds for that layer, not run until it holds a thousand such pairs or when the bundle has fewer than eight adjacent pairs; for `cdc`, `abstract` and `verbatim` layers the band does not exist and is not run". NEEDS: the calibration member's `vectors` tensor (the probe set), and for the band, the peer's own local text and its own already-held vectors for that layer. COSTS: "under a second, CPU". CATCHES: "wrong dtype or shape, shuffled or perturbed vectors". Three things an implementer must get right: the cosine PROFILE is recorded, never a gate; the realisability gate is the only always-on decision in T1; and the band's reference distribution is LOCAL and self-measured, never published by anyone.
   **source**: 03 §3.5 table
   **what**: T1 geometric — three parts, two of which are evidence not gates

3. **detail**: Quoted whole: "golden (text, vector) pairs — the calibration set's first sixteen texts and their reference vectors — re-embedded by the producer into the bundle header (09 §9.7); passes on the calibration quorum; no age rule". 09 §9.7 makes it concrete: header key `challenge` is `{set: id, n: 16}`, and "the first `n` rows of `vectors.parquet` are the calibration set's first `n` texts (§9.12) embedded by the producer, with `chunk_id` `"calibration:<i>"`, `i` from 0"; `chunk_count` is "rows in `vectors.parquet` AFTER the challenge rows". 09 §9.12: "T2's golden pairs are texts 0–15 with their rows". NEEDS: the calibration member (already held, it is a member of every snapshot). COSTS: "none for the importer". CATCHES: "misconfiguration — proof of a correct oven, not of each loaf; not a security control". FAILING: the bundle is refused. "no age rule" is explicit and must not be added back.
   **source**: 03 §3.5 table; 09 §9.7 `challenge`; 09 §9.12
   **what**: T2 challenge — the calibration set's first sixteen, in the parquet

4. **detail**: Quoted whole: "sixteen chunks chosen by the importer's private randomness, re-embedded with the importer's own compliant encoder; fails when fewer than twelve reach `tolerance` or the median is below it — the calibration quorum reused". NEEDS: local text for the sampled chunks, and this peer's encoder holding a current calibration statement for the space (03 §3.3). COSTS: "a few seconds, GPU — under one percent of production", where the header fixes production at "a bundle of ten thousand chunks whose production took about half a GPU-hour", and the paragraph adds "honest import costs about a fifth of a percent of production, so fifty peers importing one shared bundle spend, together, under a tenth of what one of them embedding it would". CATCHES: "bulk substitution: a wrong model, a wrong quantisation, a bundle mostly garbage". POWER, stated honestly by the spec: "one importer fails a half-corrupt bundle nineteen times in twenty and a bundle with a third of its vectors wrong about half the time; a bundle with a few percent of wrong vectors passes T3 nearly always and is left to T4 and to `vectors-sample` across the network. The network carries the safety, not one importer". RANDOMNESS: "The importer's randomness in T3 is private and never derived from the bundle, or a producer could re-roll a nonce until the sample missed its corruption" — so `secrets.SystemRandom`, NOT `codex.sample()`, which is the seeded-and-published sampler for `vectors-sample`. FAILING: a T3 fail is a verified failure and therefore a proof of forgery (below).
   **source**: 03 §3.5 table and the paragraph after it
   **what**: T3 spot recompute — sixteen chunks, the calibration quorum, private randomness

5. **detail**: Quoted whole: "a poisoned vector is inert until a search surfaces it; the chunks a search actually cites — those in the list it returns to its caller — are re-embedded once with this peer's compliant encoder, asynchronously, and marked *verified here* forever". COSTS: "a few seconds per answer, once per chunk ever cited". CATCHES: "targeted poisoning, which sampling cannot find". Three rules: it is asynchronous (never blocking the answer); "The T4 mark is local state, keyed on the chunk id AND the encoder that made it; it is never gossiped"; "What travels from T4 is a failure". A T4 miss becomes a proof of forgery only when it "reproduces on a second recompute" — one miss is not enough.
   **source**: 03 §3.5 table and the paragraph after the ladder
   **what**: T4 verify on use — local, permanent, never gossiped

6. **detail**: "A bundle is usable once T0, T1 and T2 pass. When T3 cannot run on this peer — no compliant encoder, or the encoder past its bounded wait — the bundle is imported and its chunks are shown as *unverified here*; the peer publishes no `vectors-sample` attestation for it, and `hold vectors check` runs T3 over every such bundle the first time a compliant encoder is available. A tier that could not run is recorded as not run, never as passed." 09 §9.10 stores that as the coverage map's `tiers` object, `{T0, T1, T2, T3}` each "pass" · "fail" · "skipped", plus `accepted_at` ("`null` while it has not" passed) and `verified_on_use` (an integer count for T4).
   **source**: 03 §3.5
   **what**: The admission rule itself, and what a skipped tier means

7. **detail**: A verified failure — "a T3 fail, or a T4 miss that reproduces on a second recompute, on a peer whose encoder holds a current calibration statement for the space" — is an attestation of check `forgery` carrying "the chunk id, the hash of the claimed vector, the hash of the recomputed vector and the encoder image, and for a red-class collection (04 §4.4) the record's `content_hash` in place of the chunk's text, which never travels". 09 §9.8's detail shape: `{chunk_id, producer: key, bundle: id, claimed: id, recomputed: id, text: string or null, content_hash: BLAKE2b-128}`, where "`claimed` and `recomputed` are `H` over the vector as big-endian `space.dtype` bytes". It burns the key LOCALLY: "the key is quarantined there, its bundles are dropped from search — their chunks return to *without vectors* and the coverage map says so — and its attestations are not counted; the quarantine is permanent until an operator runs `hold key trust <key> --restore`". Elsewhere it is only a claim: "a peer holding the record re-embeds the named chunk with its own compliant encoder, and a miss burns the key there too and yields its own proof; a peer that cannot re-embed shows the claim and counts it toward the flag of §3.6".
   **source**: 03 §3.5; 09 §9.8 `forgery`
   **what**: Proof of forgery, the burn, and its reproduction elsewhere

8. **detail**: "A producer that finds its own vectors wrong ... publishes a `withdraw` attestation naming its own bundle ids (09 §9.8); peers drop those bundles, their chunks return to *without vectors*, and nothing is burned. `hold vectors check --drop` (07 §7.3) withdraws every bundle this peer published that held a dropped vector. A miss found on a withdrawn bundle's vector is not a forgery." So the drop path must (a) empty the row per 02 §2.4 ("a zero row with label 0xFFFFFFFF"), (b) emit a `withdraw` attestation for this peer's OWN bundles only, (c) suppress forgery on already-withdrawn vectors.
   **source**: 03 §3.5; 09 §9.8 `withdraw`
   **what**: Withdraw is not forgery

9. **detail**: "Two compliant bundles for one chunk are not a conflict. The first accepted vector is stored and searched; the second is compared with it and the comparison is published as a `vectors-sample` attestation naming both producers." 09 §9.6: "The row for a chunk is the vector from the first bundle accepted for it; a later bundle for the same chunk is compared and not stored, unless the first is flagged", and the shard file's `producers` header is "the canonical JSON array of the bundle ids merged, sorted". 04 §4.5 adds the fallback: "unless the first is later flagged, in which case the next accepted producer's bundle is imported in its place".
   **source**: 03 §3.5; 04 §4.5 "Duplicates are checks"; 09 §9.6
   **what**: Duplicate bundles are not conflicts

10. **detail**: "The quorum is the Codex's `space.calibration.quorum`, `{n: 16, min_pass: 12}`: the run passes when, over n texts drawn from the set by the seeded rule of §3.2, at least `min_pass` reach cosine >= `tolerance` against their reference vector and the median of those n does too." Both conjuncts, both over the drawn n. `tolerance` is `space.tolerance`, a decimal string in the Codex, and 03 §3.1 is emphatic: "No number in this paragraph is a threshold: `tolerance` comes from calibration and from nowhere else." The cosine rule is also fixed: "A cosine is computed in f32: both vectors are upcast from the space's `dtype`, re-normalised, and multiplied in f32; there is no other precision and no storage penalty anywhere."
   **source**: 03 §3.3; 09 §9.3 `space.calibration`; 09 §9.12
   **what**: The calibration quorum, exactly, and where it comes from

11. **detail**: "A peer contributes vectors to a collection only after its own `roots` and `chunks` pass on the shards it holds and at least one `records-vs-origin` attestation — its own or another key's — exists for the snapshot (04 §4.5). Records are checked before GPU is spent; there is no vector precondition, which would be circular." 07 §7.3 adds the gate on the encoder: "`vectors build` and `share contribute` refuse (exit 2) without a passing check for this collection's space on the configured endpoint; the node runs the check before the first shard of a round." And the gate is per combination: "a new driver, a new build, a new quantisation is a new test. The worker re-runs it before every embedding session for a collection and whenever the endpoint reports a different build or model identity than the last accepted run".
   **source**: 03 §3.3; 04 §4.5; 08 §8.2
   **what**: Preconditions before a peer contributes at all

12. **detail**: "`import` refuses at T0 (exit 1 — the bundle is the failing unit) one whose `space_id` is not `<collection>`'s. A record whose `content_hash` in `possession.json` differs from this copy's has that record's rows refused and the refusal recorded — the record was re-issued since — and the import exits 0, never a failure. An archive import records the archive id as producer; `--retry` puts the archive's rejected shards back in line. `--strict` refuses a bundle whose producer is not on this peer's allow list and never admits below T3. It is a local choice and the only place the allow side decides a bundle (03 §3.5); with an empty allow list — the newcomer's (08 §8.1) — it admits nothing at all, which is why it is off by default."
   **source**: 07 §7.3
   **what**: `hold vectors import` exit codes and per-record refusal

13. **detail**: "The plan sets how many SHARDS a check samples and never how many chunks: `vectors-sample` draws sixteen per sampled shard and passes on the quorum `{n: 16, min_pass: 12}` of 03 §3.3, the same sixteen T3 draws by the importer's private randomness (03 §3.5). A plan-set count would need a scaling rule for the threshold that a creator could get backwards — raising the sample to strengthen the check and weakening it instead — and would drift from T3, whose randomness is deliberately unpublished." The difference between the two: `vectors-sample` uses the published seed and `sample(seed, 16, chunk ids of the sampled shard)`; T3 uses OS entropy and publishes nothing.
   **source**: 09 §9.3 validation block; migration plan step 4
   **what**: `vectors-sample` draws sixteen, always

14. **detail**: Four files: `header.json` ("JSON with sorted keys, no whitespace, ASCII only ... verified as the bytes received"), `vectors.parquet` ("Two columns, `chunk_id` (string) and `vector` (fixed-size list of `dim` elements of `dtype`); the challenge rows first in index order, then the chunk rows sorted by (uid UTF-8 bytes, `seq` as an integer) — a tuple sort, under which `:10:` follows `:2:`"), `possession.json`, `MANIFEST.sig` (64 raw Ed25519 bytes). Header keys, every one present and no other: version, collection, space_id, snapshot_id, shard, uids, chunk_count, dim, dtype, producer_pubkey, encoder_image, encoder_weights, content_digest, parquet_bytes, possession_bytes, created_at, digest_alg, sig_alg, challenge. `content_digest` = `sha256("louise-rag/vectors/v1" ‖ 0x00 ‖ u32be(dim) ‖ u32be(rows) ‖ per row: u32be(len(chunk_id)) ‖ chunk_id ‖ vector as big-endian dtype bytes)`. Signed payload and id exactly as the code already has them. On-disk home: `bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/`; member name `bundle-<space>-<bundle_id hex>`. Caps: "header at most 1 MiB, a string at most 4,096 characters, at most 10^6 uids"; "no floats anywhere".
   **source**: 09 §9.7
   **what**: The bundle's own wire form, which T0 parses

15. **detail**: "A coverage claim counts — for the cascade, for adoption, for §4.7 — only when the bundle it names has been fetched and passed T0–T3." Note the T0–T3 there, against §3.5's "usable once T0, T1 and T2 pass": a bundle admitted with T3 skipped is USABLE locally but its coverage claim does not count. 09 §9.10 keeps a neighbour's claim separately: "recorded beside these as `claimed_by: [key…]` per bundle and counts for nothing until the bundle is fetched and accepted here". Also 04 §4.5: "A bundle's id is the hash of its signed bytes (09 §9.7); a producer that restarts and re-emits the same bundle produces the same id, and an importer that holds that id ignores it."
   **source**: 04 §4.5; 09 §9.10
   **what**: When a coverage claim counts


### size_risks

1. 

2. 

3. 

4. 

5. 

6. 

7. 

8. 

9. 


### unspecified

1. 

2. 

3. 

4. 

5. 

6. 

7. 

8. 

9. 

10. 

11. 

12. 



## coverage-and-work â the coverage map, what counts as covered, and choosing work

Step 4's coverage dimension is almost entirely unbuilt: there is no coverage map in the code at all. What exists is a per-bundle ledger (`app/bundles/ledger.py`, `bundles` table at app/db.py:112) that has no `shard`, no `uids` range, no `tiers` object, no `source`, no `accepted_at` and no `claimed_by`; a per-document `embed_done` flag; and a `chunk_verification` table keyed per chunk. The map 09 §9.10 fixes is a different object — one row per accepted BUNDLE, keyed on the four-segment `shard_id` — and the shard concept does not reach the manifest at all (no `shard` column, no `vectors_path()` helper in app/hold/drag.py, no `shard` key in the bundle header at app/bundles/format.py:102). The rendezvous is now correct (commit 6838a41) but is a pure function with no caller, no live-peer source, no grace window and no stall penalty. Four document-vs-document disagreements and several document-vs-code ones are listed below; the most consequential are (a) 07 §7.8 and 09 §9.10 put the coverage map in two different databases, (b) 02 §2.3's "about 7,000 chunks" per shard contradicts its own PubMed figure and 09 §9.10's worked example (30,000), which changes the shard count by 4x, and (c) the migration plan's step-9 "per-document bitmap" is not 09 §9.10's per-bundle row. On size: the map is per shard and per bundle — roughly 1,200 shards and 10^4–10^5 rows for 36M PubMed records, which is small; the corpus-scaling danger is not the map but the ROUND QUERY over it, the `chunk_verification` table, and the digest's `coverage`/`members` lists, which at bundle granularity reach megabytes per hourly digest.

### disagreements

1. **problem**: The two documents put the coverage map in two different databases. 09 §9.10: "It lives in the state database (`state/manifest.sqlite`, 07 §7.8) as one row per accepted bundle". 07 §7.8's tree: `collections/<name>/state/` holds "manifest.sqlite, cursor, ack.json, attestations/, bundles/, lock" — no coverage map — while `<LOUISE_ROOT>/state/` holds "node.sqlite: the catalogue, coverage maps (09 §9.10), peers seen, runtime overrides; trust.json". The row's own shape argues for node.sqlite (the `shard` field carries the collection_id, which would be redundant in a per-collection database, and `claimed_by` can name a collection this peer has heard of but does not hold). The command surface argues for manifest.sqlite (`hold vectors show <collection>` is per-collection, and `hold collection forget` should take the map with it). This is a permanent choice — it decides which connection every writer in step 4 takes, and a wrong pick means a cross-database transaction between the bundle merge and the coverage row, which invariant 2 forbids ("a stage marks done only AFTER its durable commit, inside the same transaction").
   **suggestion**: Ask before writing. My reading: put the accepted-bundle rows in the collection's `manifest.sqlite` (so the merge and the row commit atomically, and `forget` is a file delete), and keep only the node-wide DERIVED view — the catalogue's per-collection vitality and `claimed_by` for collections not held — in node.sqlite. Then amend whichever document loses. Do not let the code decide silently.
   **where**: docs/07-commands.md:645-651 (§7.8 state tree) vs docs/09-formats.md:1051-1053 (§9.10)

2. **problem**: §2.3: "`slicing.shard_records` is chosen per collection so that a shard is about 7,000 chunks — 30,000 PubMed abstracts, 2,000 Wikipedia articles, 50 Gutenberg books." PubMed's layer is `abstract` (09 §9.10: "T1 is skipped because PubMed's layer is `abstract`, not `fixed`"), which yields at least one chunk per abstract, so 30,000 abstracts is 30,000 chunks and not 7,000 — and §9.10's own worked example says `"chunks":30000` for PubMed shard 11. The Wikipedia figure is inconsistent the other way: §9.6's worked example gives 2,000 pages → 11,806 chunks, not 7,000. The code has records_per_shard=30_000 for pubmed and 2_000 for wikipedia (app/pack/slicing.py:88,92), so the code follows the RECORD counts and the "about 7,000 chunks" claim is the odd one out.
   **suggestion**: Report it upward as a spec inconsistency rather than patching it. It matters here because `tokens` (chunks × ctx) is the eligibility input and `chunks` is the openness denominator: a 4x error in either changes who is eligible and when a shard closes. Until it is settled, take `chunks` from the snapshot's shard entry and never from a rule of thumb.
   **where**: docs/02-collections.md:307-310 (§2.3) vs docs/09-formats.md:1080 (§9.10 worked example) and app/pack/slicing.py:87-90

3. **problem**: The plan says "Coverage map: per-document bitmap per space, assembled from bundles and attestations, exchanged in digests", and the note argues at length that per-document is right and "Per shard is still too coarse". 09 §9.10 fixes something else entirely: one row per accepted bundle, keyed on the shard, with `covered` and `open` DERIVED per shard. CLAUDE.md is explicit that docs/notes/ is the engineering record and the numbered documents win. The note's argument is not wrong about the lazy path's lookup — but 09 §9.6 already answers that lookup better than a bitmap does: the shard's `labels` tensor has 0xFFFFFFFF exactly where a chunk has no vector, per chunk, for free.
   **suggestion**: Build 09 §9.10's per-bundle rows as the map, answer "is this chunk covered?" from the shard's labels tensor, and fix the plan's step-9 bullet. If a document bitmap is still wanted as a gossip form, it is a separate derived artefact and needs its own section in 09 before any byte of it travels.
   **where**: docs/notes/migration-plan.md:536-537 (step 9) and docs/notes/embedding-redundancy.md:95-135 vs docs/09-formats.md:1050-1075 (§9.10)

4. **problem**: §4.5: "A contributor emits a signed bundle every few hundred chunks, not at the end of the shard." The §9.10 worked example shows the same shard covered by two bundles of 14,800 and 15,200 chunks. Both cannot be normal. The choice is not cosmetic: at 300 chunks/bundle a fully covered PubMed collection has ~120,000 coverage rows and ~120,000 members to announce; at 15,000 it has ~2,400.
   **suggestion**: Treat "every few hundred chunks" as the rule (it is the normative sentence; §9.10's is an illustration) and size the schema and the digest for 10^5 rows per collection — then raise the digest-size problem below, because at that granularity the digest's `coverage` and `members` lists are megabytes.
   **where**: docs/04-sharing.md:452-456 (§4.5, partial publication) vs docs/09-formats.md:1080-1086 (§9.10 worked example)

5. **problem**: 07 §7.8 names the modes `off | on-demand | all`. The code names them `off | lazy | all` (EMBED_MODES, DEFAULT_EMBED_MODE = "lazy"), and "lazy" is threaded through app/db.py:345, app/worker.py:741/797/810/830, app/retrieval.py:213, app/web.py:800 and tests/test_config_tiers_embed_mode.py.
   **suggestion**: Rename to `on-demand` with `lazy` accepted as a deprecated alias in the TOML reader (the migration plan's own rule: "add the new command beside the old one with a deprecation alias"). Not a behavioural change, but 07 §7.8 says `hold config show` prints the effective value, and printing "lazy" for a spec value of "on-demand" is a lie in the one place the tiers are supposed to be legible.
   **where**: docs/07-commands.md:615,626-629 vs app/collections.py:140-141

6. **problem**: 07 §7.8: "`embed = on-demand` computes vectors lazily, FOR THE CHUNKS A SEARCH NEEDS". The code enqueues whole DOCUMENTS (the top 10 hit book_ids) and `embeddings.upsert_book` then embeds every chunk of each. For Gutenberg, where a book runs to 2,004 chunks (docs/notes/embedding-redundancy.md), "the chunks a search needs" and "every chunk of the document a search hit" differ by three orders of magnitude per hit.
   **suggestion**: Note that 09 §9.7 forces the document granularity anyway: a bundle's `uids` are "the uids whose chunks the bundle covers, EVERY CHUNK OF EACH", so a partial document cannot be published. So the code's behaviour is right and 07 §7.8's wording is loose — but say so in the commit rather than leaving two readings alive, because the difference is a GPU-hour per query on a large-document collection.
   **where**: docs/07-commands.md:626-627 vs app/retrieval.py:119-131,213

7. **problem**: The implemented header lacks `snapshot_id`, `shard`, `encoder_image` and `encoder_weights`, and carries `backend_fingerprint`, which 09 §9.7 does not list (and §9.7 says "Every key below is present and no other is"). The missing `shard` is the load-bearing one: 09 §9.10's row is keyed on a shard_id whose <slice>/<n> can only come from the bundle, and 09 §9.7 states "one shard per bundle" as a constraint the header must express. Also `content_digest` is stored as bare hex (`_check_hex(..., 32)` at format.py:308) where 09 §9.7 writes it `sha256:…`.
   **suggestion**: Step 4 must change the header. Because unknown AND missing keys both hard-fail (format.py:262,265), this is a clean break, not a migration: state it as such, and make `hold vectors import` of a v1-shaped legacy bundle a named refusal rather than a parse error.
   **where**: docs/09-formats.md:753-758 (§9.7 header keys) vs app/bundles/format.py:102-107

8. **problem**: "a key that led a shard on this peer's view and published no bundle for it within `grace_days` is not scored on that collection until a bundle from it is accepted, so an inflated number costs the collection one window, not one per shard." This is the ONLY thing bounding a self-reported benchmark, and app/share/rendezvous.py has no notion of it — `ranking()` scores every eligible peer every time. Without it, one peer publishing `tokens_per_s: 10^9` leads every shard of every collection forever and the network embeds nothing.
   **suggestion**: It is per-collection state ("not scored on that collection"), it is derived from the coverage map (who led, when the window opened, whether a bundle from them was accepted), and it therefore belongs in step 4's map, not step 9's. Add a `stalled_until` / `not_scored` set per (collection_id, key) maintained when a window expires and cleared when a bundle from that key is accepted, and have the caller filter `peers` before calling `ranking`.
   **where**: docs/04-sharing.md:439-444 (§4.5) — nothing implements it

9. **problem**: The note says "a peer that would take more than three hours on that shard is not eligible for it"; §4.5 fixes the limit as the collection's work PERIOD (2,592,000 s for P1M). Commit 6838a41 already corrected the test that had encoded the note's number.
   **suggestion**: Already resolved in code; flagging only because the note still reads the old way and a reader may re-derive the wrong limit from it. The note is the engineering record and the document wins — but it would save the next reader an hour to say so in the note.
   **where**: docs/notes/no-central-node.md:226-227 vs docs/04-sharing.md:427-434

10. **problem**: 09 §9.10 requires `hold vectors show --json` to print `vitality`, and §4.7 says signed work of BOTH kinds counts — bundles and attestations. Attestations land in step 5. A step-4 `vectors show` that counts only bundles will call a finished-and-checked collection (Gutenberg, where there is nothing left to embed) DORMANT, which is exactly the failure §4.7 was written to prevent: "A rule that counted only bundles would call a finished collection dormant and a rotting one healthy."
   **suggestion**: Either defer the `vitality` field to step 5, or print it with an explicit "attestations not yet counted" qualifier. Do not ship a number that is wrong in the direction the document names.
   **where**: docs/04-sharing.md:549-600 (§4.7) vs the migration order


### existing_code

1. **does**: The whole of 04 §4.5's partition as pure functions: Peer(key, benchmark), ShardWork(shard_id, tokens), `_unit` (:45, now H over canonical JSON of [peer_key, shard_id]), `score` (:61, ln(h)/w, raises on a zero benchmark rather than ranking last), `expected_seconds`/`eligible` (:79/:86, 3×tokens/w ≤ limit), `ranking` (:100, full ordering, ties on key), `leader`, `successor` (:114), `assign`, `mine` (:134), `fetch_leader` (:144, the synthetic "<collection_id>/<snapshot_id>/fetch" key with limit_seconds=inf), `share_of_leadership`.
   **reusable**: Reuse as is — commit 6838a41 fixed the concatenation, the formula, the shard-id arity and the unscored peer. Do NOT reimplement any of it. What it lacks and step 4 must add around it: the caller must supply `limit_seconds = seconds(codex.work.per)` (no `seconds()` helper exists anywhere in app/), the live-peer set, the grace window, and §4.5's local stall penalty (a stalled leader is not scored on that collection at all until a bundle from it is accepted) — none of which is in this module and none of which is elsewhere either. `mine()` returns shards this peer LEADS, which is not the same as shards it may work on: the cascade's window is not modelled here.
   **where**: app/share/rendezvous.py:29-162

2. **does**: The node-local bundles ledger: bundle_id PK, producer_pubkey, space_id, collection, state (applying|applied|rejected|aborted), uids as JSON {uid: chunk_count}, chunk_count, signature, parquet_sha256, content_digest, space_weak, tier_reached (a single integer, highest tier that ran and passed), verdict, error, created_at, imported_at. Indexes on producer_pubkey and state.
   **reusable**: This is the nearest thing to a coverage row and it is the right place to grow one, but it is NOT 09 §9.10's row. Missing columns: `shard` (fatal — the map is keyed on it, and it cannot be back-derived because the bundle header has no shard field either), `collection_id` in id form (the column holds a local NAME), `uids` as [first,last], `chunks` meaning rows MERGED, `tiers` as four independent verdicts (a single `tier_reached` integer cannot express T1="skipped", T3="skipped", T2="pass"), `accepted_at`, `source`, `verified_on_use`, `claimed_by`. `record_rejected` at :67 is worth preserving verbatim — its docstring records why a rejection is never written to documents.error.
   **where**: app/db.py:112-131 (`bundles` table) and app/bundles/ledger.py:26-166

3. **does**: The v1 bundle header schema as implemented: version, space_id, collection, uids, chunk_count, dim, dtype, producer_pubkey, backend_fingerprint, content_digest, parquet_bytes, possession_bytes, created_at, digest_alg, sig_alg, challenge. Unknown keys are REJECTED (a deliberate anti-strip measure, documented at :101-103).
   **reusable**: The strict-parse machinery, the domain-separated framed payload (:353 `signed_payload`) and the container-independent `content_digest` (:399) are all exactly 09 §9.7 and should be kept. The KEY SET is not: 09 §9.7 requires `snapshot_id`, `shard`, `encoder_image` and `encoder_weights`, and lists no `backend_fingerprint`. Because unknown keys are rejected and missing keys are rejected (:262, :265), this is a breaking wire change — every existing bundle becomes unreadable and every new one unreadable by old code. `challenge` is currently refused when non-null (:278) pending the T2 verifier.
   **where**: app/bundles/format.py:102-107 `_HEADER_KEYS`

4. **does**: The import path: `_check_space` (:92), `_read_parquet` (:140), `_local_chunks` (:159), `_run_ladder_if_possible` (:224), and the write order documented at :21-27 — clear embed_done=0 BEFORE the destructive Lance write, set embed_done=1 and ledger 'applied' after. Calls `ledger.mark_imported` (:476) and `ledger.record_applied` (:482).
   **reusable**: The ordering and the crash-recovery reasoning are invariant-2 correct and must survive. What changes: the write target becomes the shard's safetensors file (09 §9.6) plus the Lance rebuild, and the same transaction must write the coverage row's `accepted_at`, `chunks` (merged, not claimed) and `tiers`.
   **where**: app/bundles/importer.py:301-490

5. **does**: Per-chunk T4 state: chunk_id PK, uid, state (imported|verified|failed), producer, bundle_id, cosine, checked_at. Indexes on `state` and `uid` only. `resweep` (:168) reads chunks by bundle_id at :185.
   **reusable**: This supplies 09 §9.10's `verified_on_use` — but there is NO INDEX ON bundle_id, so computing that field is a full table scan per bundle. Add `idx_chunk_verification_bundle ON chunk_verification(bundle_id, state)`, or better maintain the count as a column on the coverage row and treat this table as the T4 record only.
   **where**: app/db.py:178-188 `chunk_verification` + app/bundles/verify_on_use.py

6. **does**: Today's embed selection: (1) the lazy queue (`embed_queue`, priority DESC) under mode lazy|all; (2) under `all`, a dense backfill over `documents` walked in rowid order from an in-memory cursor keyed on the manifest PATH, excluding id ranges a declared genesis archive can still cover (`_covered_ranges`). Cursor TTL 120s, invalidated by `genesis.coverage_epoch()`.
   **reusable**: This is the SHAPE the coverage-driven selection should keep — cursor-paged, no ORDER BY over the candidate set, no CAST in the index path — but the SUBJECT is wrong for step 4. It selects DOCUMENTS corpus-wide; §4.5 selects SHARDS, and within a shard the work is bounded by the shard. `_covered_ranges` is the closest existing analogue of "what someone else will deliver", and its epoch/cursor idiom is directly transferable to "what a peer leads". The archive coverage it models (genesis_imports.id_min/id_max) is 09 §9.10's `source: "archive"` case in embryo.
   **where**: app/worker.py:739 `_next_book`, :804 `_next_embed`, :826 `_embed_candidates`, :641 `_BACKFILL_SQL`, :652 `_covered_ranges`, :679 `_backfill_rows`, :705 `_cursor_page`

7. **does**: The embed-mode tier machinery: EMBED_MODES = ("off", "lazy", "all"), DEFAULT "lazy"; the TOML value is Tier 2 and replaces any runtime override at boot, at add and on a TOML edit.
   **reusable**: The tier resolution is invariant-10 correct and should be reused unchanged. Only the VOCABULARY must move: 07 §7.8 names the modes off | on-demand | all.
   **where**: app/collections.py:140-141, app/db.py:345, app/db.py:374 `apply_desired_embed_mode`, app/worker.py:2040 `_apply_tier2_embed_mode`

8. **does**: The on-demand path today: after fusion, the top 10 hit DOCUMENTS that are not embed_done are inserted into `embed_queue` at priority 100. The dense leg itself is deliberately NOT gated on embed_mode (:160-165, F1-D6).
   **reusable**: The F1-D6 comment is exactly 07 §7.8's "never what search uses" and should be preserved. The granularity is the problem — see disagreements.
   **where**: app/retrieval.py:119 `_enqueue_lazy_embed`, :213

9. **does**: Member names and the whole-collection paths under vectors/.
   **reusable**: Reuse `check_space` for the shard_id's <space> segment. MISSING and needed by step 4: `vectors_path(space, slice, n)` → "vectors/<space>/<slice>/NNNNN.safetensors", `buckets_path(space, v, g)` → "vectors/<space>/buckets/v<n>/g<kk>.safetensors" (k zero-padded to two digits, 09 §9.6), and the at-rest bundle directory "bundles/<space>/<slice>/<NNNNN>/<bundle_id hex>/" which lives under state/, not drag/.
   **where**: app/hold/drag.py:66 `check_space`, :87 `vectors_member`, :91 `buckets_member`, :95 `bundle_member`, :166 `centres_path`, :170 `vector_index_path`

10. **does**: The shard entry as 09 §9.4 fixes it, including `chunks`, `chunks_root` and `tokens` ("chunks × space.ctx — the upper bound §4.5 uses") and the `path` property.
   **reusable**: This is the source of `ShardWork.tokens` and of `chunks` for the openness test. `Shard.path` gives the three-segment form; step 4 needs a `shard_id(collection_id, space, shard)` helper producing the four-segment string, and it should live beside this class or in rendezvous.py so exactly one function mints it.
   **where**: app/hold/snapshot.py:28-52 `Shard`

11. **does**: Step 3's calibration gate already measures and publishes this peer's own benchmark per space, and formats 09 §9.9's `benchmarks` digest row.
   **reusable**: This is `w` for THIS peer in the rendezvous. Nothing yet PERSISTS the passing statement, so step 4 must decide where the accepted statement lives (07 §7.8 says state/) and read `tokens_per_s` from it — a build that invents a benchmark, or reuses another space's, is 04 §4.5's "unscored" case handled wrongly.
   **where**: app/hold/embedder.py:63 `tokens_per_s`, :161, :199 `benchmarks_entry`

12. **does**: The CURRENT partitioning: a central coordinator issues advisory leases over uids (GET /work at app/contribute.py:110), the contributor embeds them, exports a bundle and POSTs it back; the coordinator keeps its own bundles/credits/demand tables.
   **reusable**: Structurally opposite to 04 §4.5, which needs no messages and no arbiter. The migration plan (docs/notes/migration-plan.md:536) keeps /work as "consulted only if configured, and its answer is advisory", and that is step 9's problem, not step 4's. The reusable parts are the transfer-budget binding (`manager.bind(Priority.SEEDING, ...)` at :132, invariant 5) and the export/upload plumbing.
   **where**: app/contribute.py:94-152, app/coordinator/store.py:19-80, app/coordinator/leases.py

13. **does**: `--stored` compares each stored vector against a fresh embedding of the chunk text as chunked today; `vector_sweep` walks books in clusters and, crucially, re-draws every suspected chunk twice so a bad DRAW never condemns a healthy document, aborting the sweep if the two fresh draws disagree with each other.
   **reusable**: The double-draw safeguard is the single most important thing in either tool and must survive into `hold vectors check` — without it `--drop` deletes good vectors on the word of a misbehaving encoder. The cluster walk is a good default for `--drop` too. Both must additionally, per 03 §3.5, publish a `withdraw` attestation for every bundle this peer published that held a dropped vector, and turn the dropped row into a zero row with label 0xFFFFFFFF so the coverage map stops claiming it.
   **where**: tools/vector_compliance.py, tools/vector_sweep.py


### required_behaviour

1. **detail**: `shard` string = "<collection_id>/<space>/<slice>/<n>", the four-segment rendezvous shard_id of 04 §4.5, with <collection_id> in textual form ("sha256:"+64 hex), <space> the first 16 hex digits of space_id (09 §9.1's path-segment rule; app/hold/drag.py:66 `check_space`), <slice> the slice name ("year=2023"), <n> the shard number ZERO-PADDED to five digits as written in its path ("00011" — the worked example at 09-formats.md:1084 confirms it). The SAME string names a shard in an attestation (09 §9.8), so any deviation silently unmatches. `bundle_id` id ("sha256:"+hex of the signed payload, 09 §9.7 — derived, never a header field). `producer` key ("ed25519:"+64 hex). `uids` = [first, last], the bundle's chunk RANGE, not its list — the list lives only in the bundle header. `chunks` integer = ROWS MERGED FROM IT, not the bundle's chunk_count: a duplicate bundle whose rows lose to an earlier one contributes fewer (or zero); this is what makes `covered` = SUM(chunks) exact rather than an overcount. `tiers` object {T0,T1,T2,T3} each "pass"|"fail"|"skipped"; T1 skipped on a non-`fixed` layer, T1 and T3 skipped on a peer without the text (a red-class collection). No T4 here — T4 is `verified_on_use`. `accepted_at` time, NULL while the bundle has not passed this peer's ladder — so a row exists BEFORE acceptance, which is how a fetched-but-unverified bundle is represented. `source` = "local"|"fetched"|"archive", who produced the vectors FROM THIS PEER'S VIEW. `verified_on_use` integer = chunks of it T4 has re-embedded and confirmed.
   **source**: docs/09-formats.md:1050-1075 (§9.10)
   **what**: 09 §9.10's row, field by field — nine fields, one row per ACCEPTED bundle (not per shard, not per chunk, not per document)

2. **detail**: Never a member: it is not in the drag, never hashed into a snapshot or Codex, never fetched by torrent, and `share` must REFUSE to serve it (07 §7.8: drag/ holds only what the Codex or a snapshot names). Never signed: it carries no envelope and no key of its own; every fact in it is derived from objects that ARE signed (bundles, attestations), so it is a cache that can always be rebuilt from `state/bundles/` and `state/attestations/` — which means it needs no migration story and may be dropped and recomputed. Never the same on two peers: no code may treat a divergence as an error, no reconciliation protocol exists, and nothing may compare two peers' maps for equality. 02 §2.5 states it positively: "It is the one part of a collection that is not content-addressed, and the only part that is allowed to differ between two peers for a while." Consequence for the code: it belongs in the state database, never in drag/, and it must never appear in a hash, a signature, an attestation body, or an equivalence check (03 §3.4).
   **source**: docs/09-formats.md:1051-1053 (§9.10); docs/02-collections.md:560-566 (§2.5)
   **what**: What follows from "never a member, never signed, never the same on two peers"

3. **detail**: The sentence, verbatim: "A shard counts as covered on a peer only when that peer has fetched the bundle a claim names and it has passed T0–T3 (03 §3.5); a claim heard in a digest is a hint about where work is happening, and never by itself removes a shard from the work of §4.5." 09 §9.10 says the same operationally: "A coverage claim in a neighbour's digest is recorded beside these as `claimed_by: [key…]` per bundle and counts for nothing until the bundle is fetched and accepted here." So a claim DOES: tell this peer a bundle exists and where to fetch it; renew a leader's claim for the cascade window (04 §4.5: "the leader's claim is renewed by each bundle it publishes" — this reads on bundles PUBLISHED, which the claim evidences); populate `claimed_by`. A claim does NOT: mark a chunk covered, close a shard, remove a shard from `mine()`, count toward vitality (04 §4.7: "A coverage claim counts — for the cascade, for adoption, for §4.7 — only when the bundle it names has been fetched and passed T0–T3"), or write a vector row. Two derived quantities per shard, both local: `covered` = chunks with an accepted row, `open` = the shard has any chunk without one. NOTE the exact asymmetry: 04 §4.5 says the leader's claim IS renewed by publication, while 02 §2.5/§4.5 say a claim never removes a shard from the work — the first governs the WINDOW, the second governs OPENNESS. An implementer must keep those two separate: a shard with a live leader other than me is still open, and I still do not embed it until the window expires.
   **source**: docs/02-collections.md:568-576 (§2.5)
   **what**: What makes a shard COVERED on a peer — the quote, and what a claim does and does not do

4. **detail**: 1. REFUSALS FIRST. `EMBED_URL` (or the collection's [encoder], or platform.toml [encoder_defaults.<plugin>], in that fallback order — 07-commands.md:622-626) unset and unreachable → defer, exit 3, never an error on a document. No passing `hold embedder check` for THIS collection's space on THIS endpoint → exit 2 (07-commands.md:225-228: "`vectors build` and `share contribute` refuse (exit 2) without a passing check"; the node runs the check before the first shard of a round, and an edited [encoder] re-runs it). Licence gate: a yellow/red collection without an ack is withheld (CLAUDE.md invariant 9, app/licensing.py). Space mismatch on the vector table → per-collection refuse-to-start (invariant 1). 2. EMBED MODE (07-commands.md:615,626-629). `off` = the node spends nothing; `on-demand` = compute lazily, only the chunks a search needs; `all` = the whole held set. Explicitly: "what the node spends, never what search uses" and "Search runs over whatever vectors exist regardless of the mode." Mode is Tier 2 (the TOML) resolved through `db.apply_desired_embed_mode` (CLAUDE.md invariant 10). 3. WHAT IS OPEN. From the coverage map: a shard is open while any chunk in it has no accepted bundle. Restricted to shards this peer HOLDS THE RECORDS OF and has checked — 04-sharing.md:436: "A peer embeds only shards whose records it holds and has checked; for a red collection that means the text." 4. THE PRE-CONTRIBUTION GATE (04-sharing.md:367-373): own `roots` and `chunks` pass on the shards held, at least one `records-vs-origin` attestation exists for the snapshot (its own or another key's), and the calibration gate passed. "Records are checked before GPU is spent; there is no vector precondition, which would be circular." During §4.6's daughter window no daughter of the lineage is embedded at all. 5. ELIGIBILITY, before scoring: `3 × tokens / w ≤ seconds(work.per)`, tokens = the shard's `tokens` in the snapshot (chunks × space.ctx), w = this peer's benchmark for THIS space, seconds() counts a month as 30 days and a year as 365 (09 §9.1). A peer with no `benchmarks` entry for the space is UNSCORED, not slow. 6. RENDEZVOUS: shard_id = "<collection_id>/<space>/<slice>/<n>"; h = (first 64 bits of H(peer_key ‖ shard_id) + 0.5)/2^64; score = ln(h)/w in IEEE-754 double; highest wins, tie to the lower peer key; H(a‖b) is SHA-256 over the canonical JSON of the array [a,b]. Live = a digest arrived within the last three intervals (three hours); a key known only from another peer's digest is not scored until its own digest is seen; a peer that lied on a pull is not scored for 24 hours (04-sharing.md:150-152). 7. THE CASCADE: rendezvous yields a full ORDERING, not a winner. The leader is the top eligible peer; the window runs from the LATER of (the moment the shard became open) and (the leader's last accepted bundle); on expiry the next peer in the ordering leads and embeds only the chunks that still lack an accepted bundle. Window length = the Codex's `update.grace_days` (app/hold/codex.py:271, default 7). 8. THE LOCAL STALL PENALTY (04-sharing.md:439-444): "a key that led a shard on this peer's view and published no bundle for it within `grace_days` is not scored on that collection until a bundle from it is accepted" — per COLLECTION, not per shard, and it is what bounds an inflated self-reported benchmark. 9. EMIT: a signed bundle every few hundred chunks, merged into the shard's vector file, kept in state/bundles/ for `share`. Labels computed by the importer against the centres IT holds, never read from a bundle (09 §9.6).
   **source**: docs/07-commands.md:91,230-233,565-630 (§7.3, §7.8); docs/04-sharing.md:359-450 (§4.5); docs/02-collections.md:560-576 (§2.5)
   **what**: `hold vectors build`'s decision, in order

5. **detail**: `build <collection> [--shard <slice>/<n> | --delta ID]`. Note `--shard` takes THREE segments ("<slice>/<n>"), not the four-segment shard_id — the collection and space come from the command's collection argument. `--delta ID` embeds exactly `delta.added ∪ delta.changed` of snapshot ID — a snapshot id, never a file. With neither, the shard set is the rendezvous's; `share contribute --once` "takes the shards this peer currently leads, builds and shares them, and exits".
   **source**: docs/07-commands.md:91,230-233
   **what**: `--shard` and `--delta` are the two explicit selectors and bypass the rendezvous

6. **detail**: {collection_id, space_id, snapshot_id, chunks, covered, shards: [{shard, chunks, covered, open, leader: key|null, window_ends: time|null, bundles: [<rows>]}], producers: [{key, chunks, bundles, tokens_per: {per: duration, tokens: integer}}], vitality: "actively-held"|"dormant"}. Vitality reads THIS map for bundles and the attestations this peer holds (09 §9.8) for checks — "since signed work of either kind counts and only one of the two is recorded here." The rule: over the trailing `work.per`, if ANY single key's signed work reaches `work.tokens`, actively-held; else dormant. A bundle counts chunk_count × space.ctx; an attestation counts the snapshot's `tokens` for the shards it names (deterministic checks) or the tokens of the chunks it re-embedded, and `work.tokens` for a deterministic check.
   **source**: docs/09-formats.md:1077-1088 (§9.10); docs/04-sharing.md:549-600 (§4.7)
   **what**: `hold vectors show --json` — the map's printer, and the only place vitality is computed

7. **detail**: A `bundle` event payload is {collection, space_id, snapshot_id, shard, uids: [first, last], bundle_id, root, bytes} — everything the coverage row needs except the tiers, so a claim row can be written straight from it with accepted_at NULL. A digest's `coverage` is [{shard, bundles: [id…]}] per shard, "the bundle ids this peer has accepted through T0–T3, sorted" — a peer publishes only what IT accepted, so what it advertises is a claim to its neighbours and never a forward of someone else's claim.
   **source**: docs/09-formats.md:1015 (§9.9 event payloads), 1005 (§9.9 digest `coverage`)
   **what**: The bundle event and the digest are what feed `claimed_by`

8. **detail**: vectors/<space>/<slice>/<NNNNN>.safetensors: `vectors` [n,dim] in space.dtype, one row per chunk of record shard n IN SHARD ORDER (records in order, each record's chunks by seq); `labels` [n] u32, 0xFFFFFFFF for a row with no vector; a row without an accepted vector is ALL ZEROS. Headers: chunk_ids_sha256, `producers` (canonical JSON array of merged bundle ids, sorted), index_version, index_min, collection_id, snapshot_id, space_id, shard ("<slice>/<n>"). So "which chunks lack a vector" is answered by reading one shard's labels tensor (30,000 u32 = 120 KB), NOT by a manifest query. And note the row order mismatch: the file is in SHARD order (native_id, per slicing.order), the bundle's parquet is sorted by (uid UTF-8 bytes, seq) after 16 challenge rows — 09 §9.7's own example shows `gutenberg:1012` following `gutenberg:10125`. The merge must map, not zip.
   **source**: docs/09-formats.md:678-700 (§9.6); docs/02-collections.md:397-424 (§2.4)
   **what**: The vector file is the authority on per-chunk coverage; the map is the authority on per-bundle provenance

9. **detail**: "The row for a chunk is the vector from the first bundle accepted for it; a later bundle for the same chunk is compared and not stored, unless the first is flagged." The comparison is a `vectors-sample` attestation naming BOTH producers. This is why the map's `chunks` field is rows merged: the duplicate's row records the bundle (with its tiers and accepted_at) but contributes 0 merged chunks. When the first is later flagged (a forgery burn, or a `withdraw` attestation), the next accepted producer's bundle is imported in its place, and until then those chunks return to "without vectors" and the coverage map says so.
   **source**: docs/09-formats.md:698-700 (§9.6); docs/04-sharing.md:474-482 (§4.5); docs/03-checking.md §3.5 last paragraph
   **what**: First accepted wins; the second is compared, attested, and not stored


### size_risks

1. 

2. 

3. 

4. 

5. 

6. 

7. 

8. 

9. 

10. 


### unspecified

1. 

2. 

3. 

4. 

5. 

6. 

7. 

8. 

9. 

10. 

11. 

12. 



## archive-and-legacy â importing an archive, and importing this node's existing vectors

Step 4's two import paths are the same path with different producers, and neither can be built by wrapping `app/bundles/genesis.py` unchanged. `--archive ID` must become CONVERT-THEN-IMPORT: the peer reads the archive's vectors, re-cuts each record through the Codex's layers, fills `possession.json` from the freshly built record shards' `content_hash` (NOT from the manifest — the manifest holds `txt_hash`, the ORIGIN-bytes hash, which is a different quantity), signs a bundle per record shard under its own key, and runs T0–T3. The POC's `legacy-local` path is that same conversion with the Lance table as the archive. But the POC as the plan describes it cannot run honestly: (a) `hold collection create --encoder-image/--encoder-weights` is refused as not-built (app/louise.py:257, 273) and the calibration member `calibration/<set>.safetensors` — which is T0's envelope, T1's probe set and T2's golden pairs — has a reader and no writer, so three of four tiers have no reference to run against; (b) a truthful `legacy-local` bundle header cannot be written, because 09 §9.7 requires `encoder_image`/`encoder_weights` "equal to the Codex's, or a build attested compliant", the Lance table stores no per-row build identity, and gutenberg's rows were written by at least three builds (b10450, b10603, and the unapproved b10630); (c) `legacy-local` is not a key, and 09 §9.10's `producer` is typed `key` — the honest value is this node's own key with `source: "local"`, which means the node SIGNS the b10630 damage under its own name and is burnable for it. And the corrupt fraction will NOT "fall out at import": at 8 % collection-wide, T3's 16-chunk quorum fires with probability ~0.006; even inside a 21 %-bad book it fires ~0.23. The tier that catches it is T4/`hold vectors check` (i.e. `tools/vector_sweep.py`, which is what found it), not the import ladder. Sweep before importing, not after.

### disagreements

1. **problem**: `possession.json` binds the WRONG hash. The code maps uid → `documents.txt_hash`, which is the plugin's `raw_hash` = BLAKE2b-128 of the ORIGIN BYTES (app/plugins/gutenberg/source.py:116) — 09 §9.5's `origin_hash`. 09 §9.7 requires uid → `content_hash`, BLAKE2b-128 of the canonical JSON of `[title, [[heading,text],…]]` (app/hold/records.py:30). These differ whenever cleaning or parsing changes without the origin bytes changing, and vice versa: a re-issued origin file with identical cleaned text (a whitespace or metadata edit) would refuse vectors that are perfectly valid, and a parser change that alters the cleaned text would ADMIT vectors that no longer match the text. 03 §3.5 is explicit that content_hash is what makes admissibility work: "Revision-A vectors stay off revision-B text because the content_hash says so."
   **suggestion**: Change `possession.build_map`/`verify_map` to read `content_hash` from the record shard. Keep `raw_hash_for` for `origin_hash` and `records-vs-origin`. Add a test that a record whose origin bytes changed but whose cleaned text did not still admits its vectors, and the converse.
   **where**: app/bundles/possession.py:49 `raw_hash_for` and app/bundles/format.py:12-21 vs 09 §9.7 and 09 §9.5

2. **problem**: The header field set does not match. Missing and REQUIRED by 09 §9.7: `snapshot_id`, `shard`, `encoder_image`, `encoder_weights`. Present and not in 09 §9.7: `backend_fingerprint`. Since unknown keys are rejected outright (deliberately, so a future field cannot be stripped and still verify), the two implementations cannot exchange a bundle at all.
   **suggestion**: Bring the set to 09 §9.7 exactly. Keep the rejection of unknown keys. `backend_fingerprint`'s job is now done by `encoder_image`/`encoder_weights`, which the space check and T3 both want anyway.
   **where**: app/bundles/format.py `_HEADER_KEYS` (~line 100) vs 09 §9.7's field table

3. **problem**: A truthful legacy-local bundle header CANNOT BE WRITTEN. The Lance table stores no per-row encoder identity (app/embeddings.py:238: id, book_id, lang, seq, chapter, text, vector — and nothing else), and gutenberg's rows were written by at least three builds: b10450 served the space until 2026-08-24, b10603 from then, and the unapproved b10630 was found running 2026-08-27/28 (docs/notes/approved-encoders.md). There is no way to say which build produced any given row. Writing the Codex's digest would be a false statement signed under this node's key; leaving the field out is refused by the format. Worse: a calibration statement for b10630 WOULD PASS — the operator measured it clean at every concurrency on short texts — so "a build attested compliant" does not exclude the build that did the damage.
   **suggestion**: Do not pretend. Either (a) sweep first with `hold vectors check --drop`, then declare the surviving vectors under the encoder the node runs today with an explicit note in the evidence blob that the attribution is post-hoc; or (b) — the honest option, and the one the plan's own ground rule "nothing is re-embedded" makes expensive — accept that legacy-local bundles are NOT SHAREABLE and import them with the bundle written to `state/bundles/` but excluded from `share`. Raise this with the operator; it is a decision, not an implementation choice. Note also that this makes 03 §3.3's compliance gate demonstrably insufficient for its stated purpose, which is worth an amendment: the gate is 512 SHORT texts and the defect it must catch appears only at the maximum window length.
   **where**: 09 §9.7 (`encoder_image`, `encoder_weights` required, "equal to the Codex's, or a build attested compliant") vs the POC's `legacy-local` vectors

4. **problem**: `legacy-local` is not a key. Using it breaks the typed field, `template.archive_of_key`, quarantine (`ledger.uids_written_by` keys on `producer_pubkey`), the burn path, and 04 §4.7's vitality/work counting.
   **suggestion**: Producer = this node's own `ed25519:…` key; `source: "local"`. Record the legacy quality in the bundle header's encoder fields and in `bundle_evidence`. Amend the plan's wording.
   **where**: migration-plan.md step 6.5 ("producer `legacy-local`") vs 09 §9.10 (`producer` | type `key`)

5. **problem**: REAL BUG. `shard_of` compares `s.first_id <= native_id <= s.last_id` as STRINGS, ignoring the collection's declared order. Gutenberg and PubMed are `numeric`. Lexicographically "1012" <= "10125" <= "1013" is true; numerically 1012 <= 10125 <= 1013 is false. So `shard_of` will place records in the wrong shard whenever ids differ in digit count — which for gutenberg is most of the corpus. This is the function step 4 needs to map an archive's uids onto record shards, and the function 05 §5.5 leans on.
   **suggestion**: Take the order from the Codex's `slicing.order` and compare `order_key(native_id, order)` against `order_key(first_id, order)` / `order_key(last_id, order)`. Add a test with ids 9, 10, 100 in three shards.
   **where**: app/hold/snapshot.py:163-172 `shard_of` vs app/hold/records.py:91 `order_key` and 09 §9.3's `slicing.order`

6. **problem**: The docstring says "a linear scan is honest about that", which is true about correctness and wrong about cost. Per-uid this is O(shards). PubMed at 36M records and 30,000 records/shard is 1,200 shards → 43 billion comparisons for a full-corpus archive import. Gutenberg at 50 records/shard (app/pack/slicing.py:97) is ~1,580 shards over 79k books → 125M. CLAUDE.md: a per-item cost that depends on how much has been processed is a bug however fast it looks at fixture scale.
   **suggestion**: Build the shard list once per import into a sorted array of `order_key(first_id)` and bisect, or — better — iterate the shards in order and pull the archive's uids for each shard's range, so the walk is O(shards + uids) once and never per-uid.
   **where**: app/hold/snapshot.py:163 `shard_of` — linear scan

7. **problem**: LAYER PARAMS ARE NOT IN THE SPACE. `layers.Layer` has no `params` field, `template.default_layers` writes `Chunker(name, version, {})`, and `chunk_book` reads `config.CHUNK_TOKENS` and `config.CHUNK_OVERLAP` from the environment (app/chunking.py:19,32). Invariant 1 and 02 §2.4 say the layer's params are hashed into `space_id`; 06 §6.3 says "a chunker reads no node configuration." So two nodes with different `CHUNK_TOKENS` compute the SAME space_id and produce different chunks, and every gate that would notice — T0's chunk-id set equality, the `chunks` check, `chunks_root` — is comparing against locally-configured chunking rather than against the Codex. `importer.py:382-385` already knows this ("space_id pins CHUNK_VERSION but NOT CHUNK_TOKENS/CHUNK_OVERLAP") and compensates with set equality, which turns a silent mixed-space import into a refusal but does not fix the identity.
   **suggestion**: Strictly this is step 1's, but step 4 cannot be right without it and the POC will hash a space that does not identify its own chunker. Put `{tokens, overlap}` (and cdc's `{target, min, max}`, already constants at app/chunking.py:134-136) into the Layer's params, pass them to the chunker, and make `config.CHUNK_TOKENS`/`CHUNK_OVERLAP` defaults for the recipe rather than inputs to the chunker.
   **where**: app/layers.py:61,76 and app/hold/template.py:118 vs CLAUDE.md invariant 1, 02 §2.4 and 06 §6.3

8. **problem**: The code's id for the fixed-width chunker is `"book"`. `hold/codex.py` does not validate chunker ids (only that no two layers share one, codex.py:578), so a gutenberg Codex will carry `chunker.id: "book"` — an id 06 §6.3 does not define, hashed into `collection_id` and `space_id` forever. And 03 §3.5 gates T1's overlap band on "for `fixed` layers", so a literal implementation skips T1 on every gutenberg bundle.
   **suggestion**: Rename to `fixed` before any real Codex is written (the POC writes the first one), or add an explicit mapping. Either way, validate `chunker.id` against the four registered ids in `codex.validate`.
   **where**: app/chunking.py:203 `BOOK = "book"` vs 06 §6.3's chunker id table (`fixed`, `cdc`, `abstract`, `verbatim`)

9. **problem**: The licence gate is at the CALLER, not in the import. `worker._drain_remote_archive` checks `_license_allows` (app/worker.py:1246) and `genesis_fetch.py:264` checks the ack before fetching, but `genesis.import_archive`/`import_shard` do not, and `app/cli.py`'s `genesis import` calls them directly with no check. A corpus-resident archive (the upstash/wikipedia `local=True` shape, which fetches nothing) therefore imports ack-free via the CLI. Invariant 9 names this exact failure: "hand-populated folders and peer copies are not a bypass (observed live: a peer-copied shard was indexed and imported ack-free)". `tests/test_license_gate.py:68` tests only the worker path.
   **suggestion**: Move `licensing.require_ack(collection, use="archive import")` into the import function itself, and add a test that the CLI/`hold vectors import --archive` path refuses without an ack.
   **where**: app/cli.py:427 and app/bundles/genesis.py:832 vs CLAUDE.md invariant 9

10. **problem**: T3 draws 8, not 16. 03 §3.5 fixes sixteen and the quorum 12/16; the migration plan repeats "`vectors-sample` draws SIXTEEN chunks per sampled shard, always; the plan sets how many shards and never how many chunks." The SPRT in t3.py is a different decision rule from the quorum the spec now reuses from 03 §3.3.
   **suggestion**: 16, and the calibration quorum (fail when fewer than 12 reach tolerance or the median is below it). Keep the SPRT machinery if it is wanted as evidence, but the VERDICT is the quorum.
   **where**: app/bundles/checks.py:72 `SAMPLE_SIZE = 8` and app/verify/t3.py's SPRT vs 03 §3.5 and migration-plan step 4

11. **problem**: The two disagree by a factor of ~30, and the disagreement lands entirely on import cost, because T3 is 16 recomputes PER BUNDLE. At 10,000 chunks/bundle T3 is 0.16 % of production, as the spec claims. At 300 chunks/bundle it is 5.3 %. On this node's measured 3.6 s/chunk (app/embeddings.py:180-184: gutenberg 4656 is 5,335 chunks = 5.3 hours), a gutenberg corpus of roughly 8M chunks in 300-chunk bundles is ~26,000 bundles × 16 × 3.6 s ≈ 416 GPU-hours of T3 alone.
   **suggestion**: Ask before implementing. The defensible reading: bundles are emitted every few hundred chunks for CRASH GRANULARITY and for `share`, but T3 is run per SHARD or per import batch, not per bundle — the sample is about the producer's function, and one function does not change between two bundles of one shard, which is exactly `archive_proven`'s (genesis.py:405) hard-won lesson. Whatever is chosen, put the number in the commit message.
   **where**: 07 §7.3 / migration-plan step 4 ("a signed bundle every few hundred chunks") vs 03 §3.5's cost table ("a bundle of ten thousand chunks whose production took about half a GPU-hour")

12. **problem**: Shards are numbered from 0 in the code and from 1 in the format. `shard` is a signed header field of every bundle (09 §9.7) and a component of the coverage map's `shard` key (09 §9.10), so the off-by-one is baked into hashes.
   **suggestion**: Step 2's to fix, but step 4 must not build on the wrong base. Flag it now.
   **where**: app/pack/build.py:211 (`next_n = (done["n"] + 1) if done else 0`) vs 09 §9.4 ("the shard number within the slice, from 1"; "shards are numbered from `00001`")

13. **problem**: No coverage map exists. `bundles` has no `shard`, no `uids` range, no per-tier outcomes (only a single `tier_reached` integer), no `source`, no `verified_on_use`, no `claimed_by`. 07 §7.8 also puts coverage maps in `state/node.sqlite` (node-wide), while `bundles` is per-collection in the manifest.
   **suggestion**: New table, per 09 §9.10's field list, and decide where it lives — the spec says node.sqlite, the existing code says manifest. `hold vectors show --json` must print exactly the shape 09 §9.10 specifies, including the derived `covered`/`open`/`leader`/`window_ends` per shard and the `producers` and `vitality` blocks.
   **where**: app/db.py:112-161 (`bundles`, `genesis_imports`) vs 09 §9.10's coverage map

14. **problem**: The POC's first two steps cannot run. Step 6.1 is `hold embedder calibrate`, which is explicitly refused. Step 6.2 is `hold collection create --encoder-image/--encoder-weights`, which app/louise.py:52 refuses with "it needs migration step 3". And the calibration member — which is T0's envelope, T1's probe set and T2's golden pairs, all three (09 §9.12) — has a reader (`_read_reference`, app/louise.py:298) and no writer anywhere in the tree (`grep safetensors` finds only path helpers and that reader).
   **suggestion**: Writing the calibration member is a prerequisite of step 4, not of step 6, and the dependency graph in migration-plan.md §4 ("3 → 4") hides it inside step 3. Say so, and write it: embed the 512 texts with the named encoder, store `vectors [512, dim]` f32 and `envelope [2, dim]` f32, hash each tensor's bytes into `space.calibration.vectors` and `space.envelope`, and sign the sidecar under `louise/calibration/v1` (app/hold/envelope.py has the domain and `sign`).
   **where**: app/louise.py:257 `cmd_embedder_calibrate` → `_not_yet`; app/louise.py:273-279 expects `calibration/<set>.safetensors` that nothing writes

15. **problem**: Two problems. (a) The band cannot bootstrap: at first import the peer holds no vectors for the layer, so T1 is skipped for the whole first collection — and once it holds a thousand pairs, the band is measured over the vectors it just imported, so a systematically corrupt first import defines its own acceptable band. The spec's "not run until it holds a thousand such pairs" acknowledges the first half and not the second. (b) The implemented `check_overlap` is a different test: it compares the MEAN adjacent cosine against the bundle's own random-pair baseline, which 8-15 % contamination cannot move, whereas the spec's per-pair percentile band would flag individual pairs.
   **suggestion**: Implement the spec's per-pair band, and require the band's reference population to be vectors this peer EMBEDDED ITSELF (or that passed T3) rather than any vectors it holds. Note in the code that on a first import T1 is SKIPPED and the coverage map must say so — never "pass".
   **where**: 03 §3.5's T1 band ("the 1st to 99th percentile of the same statistic over the vectors this peer already holds for that layer") vs the bootstrap and vs app/bundles/checks.py:204 `check_overlap`


### existing_code

1. **does**: The full signed-bundle import: signature over wire bytes → header parse → trust → space → content digest → possession → local chunk agreement → ladder → coarse admission → apply. Never raises to the caller; all-or-nothing.
   **reusable**: This IS `hold vectors import <file>`. Wrap it. What must change: header fields (add `snapshot_id`, `shard`, `encoder_image`, `encoder_weights`; drop `backend_fingerprint`), possession semantics (content_hash not raw_hash), the space check (read the Codex's `space_id` instead of `spaces.gather_components`), the credits refusal (new), a per-record refusal instead of whole-bundle refusal on possession mismatch (07 §7.3: "A record whose content_hash in possession.json differs from this copy's has that record's rows refused … and the import exits 0, never a failure" — today `possession.verify_map` raises and kills the whole bundle), and the coverage-map row.
   **where**: app/bundles/importer.py:333 `_import_checked` (whole function), gate order documented at :3-6

2. **does**: The archive import as it exists: adapters per archive shape, same-function proof by quorum, per-shard ledger row as cursor and audit record, budgeted resumable passes, revisit and backoff.
   **reusable**: The read side (adapters, coverage ranges, candidate selection) is exactly what `--archive` needs and is the part that has been made to scale to 36M rows. The admission side is superseded by T0–T3. The bookkeeping (`genesis_imports`) becomes the coverage map's archive half.
   **where**: app/bundles/genesis.py:832 `import_shard`, :1264 `import_archive`, :441 `drain_next_shard`

3. **does**: Range queries over `CAST(native_id AS INTEGER)` with an explicit `INDEXED BY idx_documents_native_int` hint, applied only when the index exists.
   **reusable**: MUST be preserved verbatim. The comment records the measurement: left to choose, SQLite takes `idx_documents_scope (in_scope=?)` and filters the whole collection — 19.1 s to list one shard's eligible documents on pubmed's 40M rows, 9.5 s to count one shard's pending ones, ~28 s of every 300 s pass, on a plan that reads as indexed.
   **where**: app/bundles/genesis.py:265 `eligible_in_range_sql`, :279 `pending_in_range_sql`, :251-263 `_NATIVE_INT_INDEX` / `_hint`

4. **does**: Finds the shards worth reading from the embed candidates' own locators, walked in rowid order through the `idx_documents_scope` prefix with a scan cap — never by sweeping the shard list. Residual sets stop a shard being re-read for documents a read already could not cover.
   **reusable**: Preserve. The docstring records the cost of the alternative: one ~10 s parquet read per shard per round on a 959-shard corpus, importing nothing from 958. Note the key: `(str(collection.manifest_path), shard)` — physical path, not name.
   **where**: app/bundles/genesis.py:360 `_candidate_shards` with `_CANDIDATES_PAGE_SQL` (:296), `_residual` (:229) and `_record_residual` (:311)

5. **does**: The same-function proof: quorum (median ≥ tau AND ≥75 % of samples ≥ tau), stale-sample eviction, the three-way verdict pass/inconclusive/reject, and per-archive persistence of the proof (`proven_at`, :405 `archive_proven`).
   **reusable**: This is T2+T3 under an older name — see disagreements. Its three operational lessons must survive into T3: (1) a small failing sample under a proven function is stale documents, not a verdict; (2) the proof is per ARCHIVE, taken once, persisted — a later pass whose only eligible document was the one stale one re-ran the proof on a sample of ONE and rejected 950k already-imported vectors; (3) a rejected shard stops the archive only while nothing has proven it.
   **where**: app/bundles/genesis.py:772 `_sample_re_encode`, with constants at :43-66

6. **does**: The calibrated ladder as a library — no SQLite, no manifest, no trust store; takes vectors and an artifact, returns a Verdict with per-tier PASSED/FAILED/SKIPPED and a tier_reached.
   **reusable**: The shape is right and matches 09 §9.10's `tiers` object exactly. The `SpaceArtifact` (app/verify/artifact.py) is a pre-Codex stand-in for what 09 §9.12's calibration member now is: the envelope, the probe set and the golden pairs all come out of `calibration/<set>.safetensors`. Re-point the ladder at the member and the artifact machinery (and `_trusted_space_authors`, `find_space_artifact`) goes away.
   **where**: app/verify/ (t0.py, t1.py, t2.py, t3.py, verdict.py, __init__.py:52 `run_ladder`)

7. **does**: The artifact-free floor that always runs beside the ladder.
   **reusable**: `check_structural` and `check_degenerate` map onto T0 directly. `check_isotropy` has no home in 03 §3.5's table but is cheap and catches noise. NOTE `SAMPLE_SIZE = 8` at :72 — the spec and the migration plan both say SIXTEEN, always.
   **where**: app/bundles/checks.py:322 `run_admission_checks` (structural :125, isotropy :167, overlap :204, degenerate :252, sample_reembed :278)

8. **does**: The wire format: hash the bytes as received, never re-serialize; reject duplicate keys, floats, non-finite constants, lone surrogates; caps before allocation; `digest_alg`/`sig_alg` compared to literals and never dispatched.
   **reusable**: Keep every rule; change only the field set. The file is flagged for personal security review and the reasoning in its docstring is the reason each rule exists.
   **where**: app/bundles/format.py (signed payload, strict header parse, `_HEADER_KEYS` ~:100), app/bundles/export.py, app/bundles/keys.py

9. **does**: T4 state, keyed on chunk_id. Absence means locally embedded, so the table stays proportional to imported content rather than to the corpus.
   **reusable**: This is exactly 09 §9.10's `verified_on_use` counter's backing store and the T4 mark's home ("local state, keyed on the chunk id and the encoder that made it; never gossiped"). Note the genesis path already writes these rows for archive imports (genesis.py:971) precisely so the Ask drawer stops badging imported chunks "local ✓".
   **where**: app/bundles/ledger.py:170 `mark_imported`, :184 `clear_verification`, :190 `set_verified`; app/db.py:178 `chunk_verification`

10. **does**: `--stored` compares each stored vector against a fresh embedding of the chunk text as chunked TODAY; the sweep walks corruption clusters (test one book, skip `--stride` on a good one, walk left and right from a bad one after a skip) and deletes a bad book's rows plus `embed_done`.
   **reusable**: These two become `hold vectors check [--drop]`. The SAFEGUARD in vector_sweep.py:24-38 is not optional and must survive: a mismatch alone does not say which side is wrong, so every suspected chunk is re-drawn twice — two fresh draws agreeing with each other and disagreeing with stored condemns the stored vector; two fresh draws disagreeing with EACH OTHER aborts the sweep rather than delete on the word of an unreliable encoder.
   **where**: tools/vector_compliance.py and tools/vector_sweep.py

11. **does**: The platform registry seam: archive id → Codex `Credit` (copied whole, sorted by archive id), producer key → archive id, and 03 §3.5's key-side refusal.
   **reusable**: `credits_admit` is the bundle-side refusal, complete. The `--archive`-side refusal (does `credits` name THIS archive id) does not exist and is three lines.
   **where**: app/hold/template.py:57 `expand_archives`, :78 `archive_of_key`, :95 `credits_admit`

12. **does**: 09 §9.5's hashes, and a seek-indexed reader that resolves a uid to its record through the shard's skippable-frame index.
   **reusable**: `read_record(path, uid)` is how `possession.json` gets its content_hashes; for a whole bundle prefer one `iter_records` pass over the shard (O(shard), not O(corpus)).
   **where**: app/hold/records.py:30 `content_hash`, :68 `chunks_root`, :85 `chunk_id`, :91 `order_key`; app/pack/records.py:268 `read_record`, :216 `iter_records`, :171 `read_index`

13. **does**: Cross-process serialization of the Lance delete+add pair, and the index maintainer's stand-down signal that vector writers must honour as a deferral.
   **reusable**: Both. `worker._vector_writes_allowed` is the existing check; an import that ignores it races a compaction (genesis.py:1170 catches `vector_store_contended` and converts it to a deferral — keep that).
   **where**: app/bundles/writer_lock.py `vector_write_lock`, app/vector_index.py:47 `busy` / :52 `maintenance`

14. **does**: uid→shard resolution and the member-name/path vocabulary.
   **reusable**: `shard_of` is the mapping step 4 needs, with two defects (see disagreements). `drag.py` has NO per-shard vectors path — `vectors/<space>/<slice>/<NNNNN>.safetensors` (09 §9.6) must be added, and it is a LOCAL DERIVED FILE, never a member (07 §7.8, 09 §9.16: `vectors-<space>-<slice>` is a name only and resolves to bundles).
   **where**: app/hold/snapshot.py:163 `shard_of`; app/hold/drag.py:87 `vectors_member`, :166 `centres_path`, :170 `vector_index_path`


### required_behaviour

1. **detail**: 1. RESOLVE the archive id in the platform registry (app/hold/template.py:57 `expand_archives`, backed by `genesis.ARCHIVES` at genesis.py:206). Unknown id → exit 2. 2. CREDITS REFUSAL, before any byte is read: the collection's Codex `credits` must name that archive id, else REFUSE — the Codex is immutable and a debt never declared may not be incurred (06 §6.6, 02 §2.7). `hold/template.py:95 credits_admit` implements the KEY→archive direction of this for bundles; the `--archive` direction is a direct `any(c.archive == ID for c in codex.credits)` and does not exist yet. 3. LICENCE ACK (invariant 9). Must be checked INSIDE the import function. Today it is checked only at the worker caller (app/worker.py:1246 `_license_allows`); `app/cli.py:427` calls `genesis.import_archive` with no ack check at all, and `genesis.import_shard` never calls `licensing.require_ack` — only `genesis_fetch.py:264` does, which covers the download and not the import. A hand-populated or corpus-resident archive therefore imports ack-free today. 4. FETCH/LOCATE the shard bytes (`genesis_fetch` for remote, corpus files for local). 5. READ the archive shard through its adapter (genesis.py `_ADAPTERS`, line ~752) into (uid, [vector per chunk]). 6. MAP each uid to the SNAPSHOT's record shard, because 09 §9.7 fixes one record shard per bundle and `shard` is a required header field. 7. Per record shard: re-derive chunks via `layers.chunks_for_document`, refuse the record whole if the bundle's chunk-id set ≠ the locally derived set (importer.py:388-396 already does this; genesis.py:1088 does the weaker count-only version). 8. Build `possession.json` = uid → `content_hash` from the record shard just built (`app/pack/records.read_record`), NOT `txt_hash`. 9. Add the T2 challenge rows: calibration texts 0–15 embedded by THIS peer, `chunk_id` `calibration:<i>`. 10. Sign under this peer's key (09 §9.7 payload, `app/bundles/format.py signed_payload`); `bundle_id = sha256(payload)`. 11. Run T0→T3 in order, each gating the next. 12. Write, in one durable step per bundle: the Lance rows (delete-then-insert on book_id), the drag's `vectors/<space>/<slice>/NNNNN.safetensors` merge, the bundle directory under `state/bundles/`, `chunk_verification` rows marking the chunks `imported`, and ONE coverage-map row. 13. RECORD: the coverage-map row of 09 §9.10 with `producer` = the archive's key or this peer's, `source: "archive"`, `tiers: {T0..T3}` each pass/fail/skipped, `accepted_at`, `uids: [first, last]`, `chunks`, `verified_on_use: 0`. 07 §7.3 line 243: "An archive import records the archive id as producer."
   **source**: 03 §3.5; 06 §6.6 step 5; 07 §7.3 (line 236-243); 09 §9.7; app/bundles/importer.py:3-6 and :333
   **what**: `hold vectors import --archive ID` — the whole path, in order. Gate order is load-bearing and must stay cheap-to-expensive.

2. **detail**: MedCPT is a directory of .npy files on an FTP server (genesis.py:175, `keys=()`), so it can supply neither `MANIFEST.sig` nor the T2 challenge rows. If the ladder ran on the archive's bytes as-is, T2 would always be SKIPPED, and 03 §3.5's "A tier that could not run is recorded as not run, never as passed" plus "usable once T0, T1 and T2 pass" would make every archive permanently unusable. The conversion resolves it: the converting peer produces the challenge rows with its OWN encoder, so T2 proves the oven that T3 will then use, and the same-function question T2+T3 answer together is exactly `genesis._sample_re_encode`'s question. Consequence to state plainly in the code and the report: after conversion the bundle is this peer's work under this peer's key (01 promise 3), so a later proof of forgery burns THIS peer's key, not NCBI's.
   **source**: 06 §6.6 ("An archive that publishes no signed bundles at all — a directory of .npy files, a dataset on a model hub — has an empty key set … What a peer converts from such an archive it signs with its own key, and those bundles are its own work under its own name"); 09 §9.7 (`producer_pubkey`, `challenge`, `MANIFEST.sig` all required); 03 §3.5 ("A bundle is usable once T0, T1 and T2 pass")
   **what**: An archive that publishes nothing signed must be CONVERTED into locally-signed bundles before the ladder can run. This is the only reading that makes 06 §6.6 and 09 §9.7 consistent.

3. **detail**: Source of the vectors: LanceDB table `chunks` at `collection.lance_dir` (`DATA_DIR/lancedb` for the pre-hold gutenberg, `<LOUISE_ROOT>/collections/<name>/lance` for the hold collection — DIFFERENT DIRECTORIES; the POC must read the OLD one and write the NEW one, or point the new collection at the old lance dir, and the plan says neither). Row schema: `id` (chunk id `<uid>:<seq>:<version>`), `book_id` (int native_id), `lang`, `seq`, `chapter`, `text`, `vector`. Read per shard with `tbl.search().where("book_id IN (…)").select(["id","vector"])` — never per document (embeddings.py:146 `_book_filter`; a Lance filter scans the table, and per-document filtering over 79k books is the quadratic trap genesis.py:930 documents). What a bundle over them would contain: `space_id` = the new Codex's; `collection` = the new collection_id; `snapshot_id` = the POC snapshot; `shard` = `<slice>/<n>` from `hold snapshot create`; `uids` = the shard's uids that have complete vector coverage; `dim`/`dtype` from the table; `encoder_image`/`encoder_weights` = UNKNOWABLE (see disagreements); `challenge` = 16 calibration rows embedded now; `producer_pubkey` = this node's key; `possession.json` = uid → `content_hash` read back from the record shard `hold records build` just wrote. Can possession.json be filled from the manifest? NO. `documents.txt_hash` is the plugin's `raw_hash` — BLAKE2b-128 of the ORIGIN BYTES (app/plugins/gutenberg/source.py:116), which is 09 §9.5's `origin_hash`, not `content_hash` (BLAKE2b-128 of canonical JSON `[title, [[heading,text],…]]`, app/hold/records.py:30). It is also NULL for most rows on a freshly populated node (app/bundles/possession.py:7-13). And filling it from the shard makes the T0 possession comparison tautological — it proves nothing on this path; the real gate on the POC is step 7's chunk-id set equality.
   **source**: migration-plan.md step 6.5; app/embeddings.py:23,238; app/hold/records.py:30; 09 §9.7
   **what**: THE POC PATH: `legacy-local` is that same conversion with `embeddings.TABLE` as the archive. Concretely what it must do.

4. **detail**: T0 — CAN run for dim/dtype/unit-norm/NaN/dupes (`embeddings._embed_batch` normalises client-side at line 87, so norms are 1.0) and for the chunk-id set equality per record, which is the one T0 sub-check that will actually reject material (any book re-issued or re-chunked since it was embedded). The ENVELOPE half (bundle mean cosine ≥ 0.9 to the envelope mean, per-dim sd within [0.5, 2]×) CANNOT run: the envelope lives in the calibration member, which no code writes. It is also unmeasured whether a 512-short-text envelope admits 4,000-char literary prose at cos ≥ 0.9 — measure it before turning it into a gate, or it refuses every honest gutenberg bundle. T1 — the realizability triples can run. The `fixed`-layer overlap band CANNOT, twice over: the band is "the 1st to 99th percentile of the same statistic over the vectors this peer already holds for that layer, not run until it holds a thousand such pairs", and at first import this peer holds NONE for the new collection; once it holds them they ARE the imported (possibly corrupt) vectors, so the band is measured on the population it is meant to police. Also gutenberg's chunker id in code is `"book"` (app/chunking.py:203), not 06 §6.3's `"fixed"`, so a literal `if layer.chunker.id == "fixed"` skips T1 on gutenberg forever. T2 — CANNOT run truthfully: no 2026-07 producer exists to have embedded the golden pairs. Rows embedded now prove today's oven, which is the right thing to prove about the CONVERTER and the wrong thing to prove about the vectors. Record it as what it is. T3 — CAN run: text is in the Lance `text` column and re-derivable from the record; needs a passing `hold embedder check` (07 §7.3 line 227: `vectors build` and `share contribute` refuse without one) and a `tolerance` from `hold embedder calibrate`, which is `_not_yet` (app/louise.py:257). T4 — works as designed and is where the real damage surfaces.
   **source**: 03 §3.5 table; 09 §9.12; app/verify/t0.py, t1.py, t2.py, t3.py
   **what**: Which tiers can actually run on `legacy-local`, tier by tier.

5. **detail**: The referent: on 2026-08-28 `tools/vector_compliance gutenberg` over 163 sampled chunks found 13 below TAU (8.0 %), worst cosine 0.338, median 1.000000; EVERY failure was a 4,000-char chunk (= `config.CHUNK_TOKENS*4`, the maximum window, app/chunking.py:19); failures cluster by book (4643 and 4639 at 21 %, neighbours at 0 %); the STORED side is the corrupt one (six fresh draws agreed with each other at 1.000000 and disagreed with the stored vector identically each time). Cause: the production Arc was serving `server-intel-b10630`, a build in no approval row — the approved one is b106**03**, digits transposed. Which tier catches it: T3 fires when fewer than 12 of 16 reach tolerance, i.e. when ≥5 of 16 sampled chunks are corrupt. Binomial(16, p): at p=0.08 → P(≥5)=0.006; at p=0.15 (inside a bad book) → 0.079; at p=0.21 (book 4643) → 0.234. The median of 16 stays 1.000000 throughout, so the median clause never fires. 03 §3.5 says this itself: "a bundle with a few percent of wrong vectors passes T3 nearly always and is left to T4 and to vectors-sample across the network." T0 is aggregate and blind to it. T1's band would in principle flag it — a corrupt vector breaks BOTH its adjacent-pair cosines, and the corruption is on the longest chunks, which are exactly the `fixed` windows that overlap by construction — but the band cannot bootstrap (above), and the currently implemented `check_overlap` (app/bundles/checks.py:204) is a MEAN test that 8-15 % contamination cannot move. T2 is not a security control by its own definition. So: the tier that catches it is T4 (verify-on-use, per cited chunk, forever) and `hold vectors check [--drop]`, which is exactly `tools/vector_compliance` + `tools/vector_sweep` — the tools that found it. RECOMMENDATION for the POC: run `hold vectors check --drop` (the sweep, with its two-fresh-draws safeguard and its cluster walk) over the OLD table BEFORE the legacy-local import, and import the survivors. That inverts the plan's step-6 ordering and is the only ordering that keeps corrupt vectors out of a bundle this node signs.
   **source**: docs/notes/STATUS.md:1001-1016; docs/notes/approved-encoders.md:36-58; docs/notes/lineage.md:118; tools/vector_sweep.py; tools/vector_compliance.py
   **what**: Gutenberg's "known corrupt fraction": what it refers to, and which tier catches it. It is NOT the import ladder.

6. **detail**: The b10630 defect appears only on long inputs: "the endpoint tested clean afterwards at every concurrency (1/4/8), with identical and with distinct long texts, serially and in parallel — all 1.000000. Short tests do not catch this, which is precisely why the approval trial (16 texts) and the backend gate (a handful of probes) both pass while the corpus takes damage." A uniform draw of 16 chunks from a gutenberg shard is mostly short chunks. Draw the 16 with the length distribution weighted toward the maximum window, or draw 8 uniform and 8 from the longest decile, and record the rule in the evidence blob. The spec does not forbid this — it fixes only that the randomness is private and never derived from the bundle.
   **source**: 03 §3.5 ("sixteen chunks chosen by the importer's private randomness"); docs/notes/approved-encoders.md:52-58
   **what**: T3's sample must be stratified by chunk length, or it will not see this class of defect at all.

7. **detail**: `source` = "local". The field means "who produced the vectors from this peer's view", and this peer produced them — badly, on an unapproved build, but locally. "archive" is wrong (there is no archive and no `credits` entry to justify one, and an uncredited archive would be REFUSED by 06 §6.6), and "fetched" is wrong (nothing was fetched). `producer` must be an `ed25519:…` key, and the only honest one is this node's own key at NODE_KEY_PATH. The literal string `legacy-local` breaks the typed field and, worse, breaks everything keyed on producer keys: `template.archive_of_key`, the quarantine/burn path (`ledger.uids_written_by`, `providers.quarantined`), and 04 §4.7's vitality count. Consequences to state in the POC report: (1) these vectors count as this peer's signed work toward `work.tokens` — work done with an unapproved build; (2) a later T4 miss on one of them is a proof of forgery against THIS node's key, quarantining it on the finder; (3) the mitigation the spec provides is `withdraw` (09 §9.8) — a producer that finds its own vectors wrong publishes a withdraw attestation naming its bundle ids, peers drop them, and nothing is burned; `hold vectors check --drop` is specified to do exactly this (03 §3.5). So the POC must either sweep first, or be prepared to withdraw. The `legacy` fact belongs in the bundle header's `encoder_image`/`encoder_weights` and in the evidence blob, not in the producer field.
   **source**: 09 §9.10 (`source`: "local"|"fetched"|"archive"; `producer`: type `key`); 07 §7.3 line 243; 04 §4.7; 09 §9.2
   **what**: What `legacy-local` means for the coverage map's `source` and for the producer key.

8. **detail**: Default import ignores the allow list entirely — "an empty allow list, which is what an honest newcomer has, admits every producer in existence." The DENY side is not an admission rule but a mute and applies always (a denied key's traffic is refused wholesale, bundles included), as does quarantine. `--strict` refuses a producer not on the allow list AND never admits below T3 (so a bundle whose T3 was skipped is refused under `--strict` even from an allowed producer). `app/bundles/trust.py check_admission(allow_first_use=…)` is the existing seam; note its default `allow_first_use=True` (importer.py:302) is the correct non-strict behaviour and must not be inverted.
   **source**: 03 §3.5; 07 §7.3 line 244-248; migration-plan step 4
   **what**: `--strict` and the trust list: the allow side is the ONLY place it touches a bundle, and it never applies to an archive.

9. **detail**: The unit is THE BUNDLE. The existing importer's ordering is the one to keep: ledger row `applying` committed BEFORE any destructive Lance call; then `embed_done=0`; then writer lock, per-uid delete+add; then `embed_done=1` + ledger `applied` in one transaction. `ledger.sweep_stale` at startup moves orphaned `applying` rows to `aborted` (ledger.py:119) and needs no compensating action precisely because `embed_done` was cleared first. Genesis's `flush()` (genesis.py:923) has the equivalent for the archive path plus two hard-won details: ONE Lance delete per batch of 500 documents (not one per document — a per-document delete over a growing table was the quadratic trap, measured at ~150 documents/s on pubmed), and ONE SQLite transaction per flush under `BEGIN IMMEDIATE` (the autocommit connection was doing 2×500 fsyncs per batch, a million per MedCPT shard).
   **source**: CLAUDE.md invariant 2; 07 §7.5 ("`vectors import`, `share contribute` | the bundle, marked applied in the same transaction as its last row"); app/bundles/importer.py:20-28
   **what**: The write must be idempotent and marked only after the durable commit, and the coverage-map row is part of the same transaction.


### size_risks

1. 

2. 

3. 

4. 

5. 

6. 

7. 

8. 


### unspecified

1. 

2. 

3. 

4. 

5. 

6. 

7. 

8. 

9. 

10. 

