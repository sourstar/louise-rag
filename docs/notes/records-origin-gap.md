# The record's `origin` locator — FIXED 2026-09-10

**Found 2026-09-10 implementing step 5's `records-vs-origin`; fixed the
same day by operator decision (option 1 below).** Kept as the record of
what was wrong, because a second divergence in the same list — `layer`
and `chunks` — is still open and is settled by the same reasoning.

## What was wrong

09 §9.5 makes `origin` a **required** field of every record line:

> `origin` | string | yes | the locator of this record's own origin
> bytes: their path relative to the origin base or their URL, and where
> the document is a member of a container the path followed by `#` and
> the plugin's member index (06 §6.4); never an absolute local path

`app/pack/records.py::RECORD_KEYS` does not carry it:

```python
RECORD_KEYS = ("uid", "native_id", "lang", "title", "creators", "date",
               "subjects", "keys", "sections", "extra", "layer", "chunks",
               "content_hash", "origin_hash")
```

and `canonical_line` writes `{k: record.get(k) for k in RECORD_KEYS}`, so
even a builder that produced the key would have it dropped on the way to
the file. `app/pack/build.py::_record_of` does not produce it either.

**No record this platform has ever written says where its origin unit
is.** `origin_hash` is written; the locator that hash is *of* is not.

## Why it matters, in the documents' own terms

CLAUDE.md invariant 11 is one sentence with two halves:

> every record carries `origin_hash` — the hash of the origin unit it was
> parsed from — and `origin` **locates that unit**, while the Codex names
> the acquisition spec and parser version that produced it (docs/02 §2.3,
> docs/06 §6.4), so `records-vs-origin` can re-fetch a sample and
> reproduce it.

The first half is implemented. The second is not, and without it the
clause after the comma is false.

The consequence is not "one check is weaker". It is asymmetric in exactly
the direction that matters:

- the peer that **built** a shard can still re-parse it, because its own
  `state/manifest.sqlite` holds each document's path;
- a peer that **received** that shard cannot, ever — it has the records
  and no way to know which origin bytes each came from.

03 §3.4's whole argument is that a collection received from a stranger
can be checked against its origin, and `records-vs-origin` is the check
that argument names. 03 §3.6 puts it in the counts for **checked** and
**verified**: "at least one of them passed `records-vs-origin` on a
sample" and, for verified, "the union of their samples covers a quarter
of shards for `records-vs-origin`". As things stand no peer but the
producer can supply either.

It is also the invariant CLAUDE.md marks *"Enforced: build + test
(tests/pack/test_origin_fidelity.py)"* — and that test does not assert
`origin`, which is how this survived.

## What it costs to fix

Adding one key to `RECORD_KEYS` changes:

1. every record **line**'s bytes, therefore
2. every shard file's bytes and so its BitTorrent v2 `root`, therefore
3. every snapshot that lists those shards, therefore
4. every signature over those snapshots.

**It does NOT change `records_root`**, which is computed over the
records' `content_hash` values in order (09 §9.5) and `content_hash` is
over title and sections alone. Nor does it change `chunks_root`, chunk
ids, or any vector. So the blast radius is the shard FILES and what
points at them — the transport layer — and not the content identity.
Rebuilding is a re-serialisation, not a re-parse of the corpus, and no
vector has to be recomputed.

Nothing on this branch depends durably on the current bytes — step 6 has
not run, and every drag here is rebuilt from a corpus that is still on
disk. **The legacy packs are the question**: `RECORD_KEYS` is shared with
`app/pack/`, which is what the node's live collections are built from and
what step 8 migrates. Changing it changes their record lines too.

## The options

1. **Add `origin` to `RECORD_KEYS` and to `_record_of`, and rebuild.**
   Brings the code to §9.5. The locator is already in hand at build time
   — `plugin_io.canonical_doc` takes `row["path"]` — so the change is
   small; the cost is entirely in what has to be rebuilt. This is the
   only option that makes a received shard verifiable.
2. **Add it for Codex collections only**, leaving the legacy pack writer
   alone until step 8 rebuilds them anyway. Two record shapes in one
   function is how the shapes drift, so if this is chosen it wants a
   `record_line`-style discriminator rather than a flag.
3. **Amend 09 §9.5 to make `origin` optional.** Sound only if the
   documents give up `records-vs-origin` for received shards — which
   means giving up 03 §3.4 and two rows of §3.6's table. Recorded as an
   option because it is one, not because it is a good one.

## What landed (option 1, operator, 2026-09-10)

- `RECORD_KEYS` gains `origin`, in 09 §9.5's position: after `extra`,
  before the two hashes.
- `plugin_io.origin_locator(plugin, locator, base)` does the conversion a
  locator needs before it can be a record field, and the conversion is
  the point rather than a formality: a plugin's locator is an absolute
  path on **this** machine, and `/mnt/ssd4/corpora/gutenberg/1/3/4/1342`
  locates nothing on the peer that receives the record. Relative to the
  origin base and posix-spelled, it locates the same bytes for everyone
  who fetched from the same origin.
- A container member's separator is normalised to `#`. PubMed writes
  `::` internally; §9.5 fixes one spelling on the wire, because the
  record is read by verifiers that do not have this plugin. The
  separator is read as a plain attribute (`LOCATOR_SEP`) rather than
  added to the plugin protocol — the protocol version is inside
  `collection_id` (06 §6.1), so a method there would move every
  collection's identity.
- `check_records_vs_origin` now re-parses byte for byte against an
  origin base given by `--origin DIR`, and 1931 tests pass, the whole
  legacy pack suite included.

Without `--origin` the check is **inconclusive, never a pass**: 03 §3.2's
`needs` column is "the origin, the platform image", and a check that
passed for want of an origin would certify the copy against itself.
Re-parsing the corpus directory we kept is the weaker form and is what an
offline peer can do — it proves the parser is deterministic and that the
line matches the bytes on disk, not that those bytes are still what the
origin serves. `origin_hash` is what makes even that meaningful.

## Still open: `layer` and `chunks`

The same list carries two more keys that are in **neither** 09 §9.5's
table nor 03 §3.2, which says a document's layer is derived "from the
Codex's rules, never reading it from the record, **which carries none**"
(02 §2.3, 06 §6.3). Ours carries one.

Nothing in the new code reads them — `app/layers.py` derives the layer,
as invariant 1 requires. What does read `layer` is the LEGACY pack
verifier (`app/pack/verify.py`, `app/cli.py`) and the `packed` plugin, all
of which retire with the old names at step 12. So the shape of the fix is
clear and its timing is not urgent: drop both keys when the legacy
verifier goes, in the same rebuild if one is happening anyway.

It is recorded here rather than fixed because removing them today breaks
a command that still works, and because the operator's decision above was
about `origin`.
