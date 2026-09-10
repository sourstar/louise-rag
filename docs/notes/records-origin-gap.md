# The record's `origin` locator is missing, and it is half of invariant 11

**Found 2026-09-10, implementing step 5's `records-vs-origin`. Not fixed:
fixing it changes the record bytes, and that is the operator's call.**

## The finding

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
2. every shard's `records_root` and its BitTorrent v2 `root`, therefore
3. every snapshot that lists those shards, therefore
4. every signature over those snapshots.

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

## What is in the code meanwhile

`app/hold/check.py::check_records_vs_origin` runs, counts the sampled
records, finds none of them locatable, and reports **inconclusive** with
this note's name in the message. It does not pass. A check that passed
here would certify records nobody could ever verify against their source,
which is the artefact invariant 11 calls "the worst artefact we could
ship".
