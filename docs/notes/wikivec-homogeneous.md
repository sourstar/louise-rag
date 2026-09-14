# Making wikivec and wikipedia one homogeneous entity

> **Superseded 2026-09-14** by the operator's restated plan,
> [`wikipedia-two-layers.md`](wikipedia-two-layers.md): ONE collection
> under the `wikipedia` plugin, wikivec's eleven languages numbered as
> wikivec numbers them, layer 1 by the archive's declared coverage and
> layer 2 from the dumps. The "settled" plugin `embedded` and
> `field:extra.cut` rule below were chosen to keep 91.2M legacy chunk ids
> byte-identical. That reason no longer binds: under the Codex route,
> `hold vectors import --archive` reads the archive's parquet into the
> new drag either way.

Supersedes the "blocked" verdict in
[`wikivec-merge-blocked.md`](wikivec-merge-blocked.md) — not because the
blockers were wrong, but because the operator asked a different question
and it has a cheaper answer than the one that was blocked.

**The ask, 2026-09-11:** convert wikivec's corpus into the wikipedia
plugin's format so the corpus is one homogeneous entity; layer the new
embeddings on top of the old; the new embeddings are a *daughter* of
wikivec; new articles must be re-embeddable on small changes without
re-embedding everything. And: don't change the specs.

Surveyed across five areas with the findings verified against the code.
Three of the four parts have an answer that costs nothing. One word has
to go.

---

## 1. The number everything scales by is 91.2M, not 14.2M

`app/vector_index.py:244`, measured with the app stopped on 2026-09-10:
`wikivec 91,238,804 rows, 208.7 GiB data, 63.0 GiB _indices`. STATUS.md
explains it — the `verbatim` layer is one chunk per PARAGRAPH, about
seven per article, over 12.8M articles. Every cost below is against
91.2M chunk rows.

## 2. Homogeneity costs zero vectors — via `packed`, not via re-keying

`app/plugins/packed/source.py:8-16` states the contract:

> the document's UID is the record's uid, so `source_id` is the ORIGIN
> plugin's … the chunker is the origin plugin's (or the pack's declared
> layers), so chunk ids are identical — vectors embedded from the origin
> apply to the pack and vice versa.

So: run `hold records build` over wikivec (the migration step every
collection gets anyway) and read the result back with `packed`. Both
collections become record shards — one format, one homogeneous entity —
and **not one of the 91.2M chunk ids changes**. No vector is touched, no
possession is re-proved.

The alternative, making the uid prefix literally read `wikipedia:`, is a
209 GiB rewrite of seven SQLite tables, one Lance table and one Tantivy
index, and it is worse than expensive:

* **The uid is not just a prefix.** `embedded` encodes the language:
  `native_id = LANG_ORDINAL[lang] * 10_000_000_000 + article_id`
  (`app/plugins/embedded/source.py:113-121`), while `wikipedia` uses the
  bare page id (`wikipedia/source.py:155-159`). `embedded:12` ↔
  `wikipedia:12` only for English; for the other ten languages the number
  differs too.
* **A translation table is not available.** docs/06 §6.1 fixes
  `uid = "<plugin>:<native_id>"` and docs/09 §9.3 makes `plugin` the uid
  prefix, hashed into `collection_id`. The uid keys the record line,
  `possession.json`, a bundle header and the coverage map — a local map
  would make this node disagree with every peer and nothing would detect
  it. That is a wire-format change, which the operator excluded.

## 3. "Daughter" is the one word that cannot stand

Not a preference — the validator refuses it twice, in the spec's own
words:

* `app/hold/codex.py:946` — `if codex.plugin != mother.plugin: raise
  CodexError("a daughter continues the same plugin")`.
* `app/hold/codex.py:941-943` — `if codex.space_id != mother.space_id:
  raise CodexError("a daughter keeps her mother's space — same encoder,
  layers, cleaning and epoch; this is a sister in disguise")`, and
  **layers are inside `space_id`** (docs/09 §9.3, CLAUDE.md invariant 1).

docs/02 §2.7:686 says why: "Vectors survive because the space is
unchanged — the daughter keeps the mother's `space` block, `layers` and
`cleaning_version` byte for byte." A daughter is *defined* as the
relation where the space does not change, so it is the one relation that
cannot carry a new layer.

Adding a layer therefore makes a **sister** (`derived_from`). Under
CLAUDE.md — "the documents win until changed" — and the operator's own
"don't mess with the specs", the relation is a sister and the word
"daughter" is dropped. Two consequences worth knowing:

* a sister's `lineage_id` is her own, so the catalogue shows two
  lineages joined by `derived_from`, not one entry with a history;
* `app/hold/collection.py:460-463` drops the archive credit when the
  space changes, so the recipe **must** pass
  `--archives upstash-wikipedia-2024-06` or the Upstash vectors can never
  be imported into the sister at all.

**The corollary that matters most:** wikivec has no Codex yet. Nothing
about it is frozen. Its layer list must be decided *before*
`hold collection create` runs, because after that Codex exists, adding a
layer is a sister with no inherited vectors.

## 4. The layer plan as written does not partition anything

`[[layers]] rule = "archive:upstash-wikipedia-2024-06"` puts the WHOLE
corpus in the verbatim layer and the `default` layer never fires.
`GenesisArchive.coverage()` returns `"all"` when `ids_per_shard` is unset
(`app/bundles/genesis.py:148-153`), the Upstash entry sets neither it nor
`shards` (`genesis.py:196-214`), and `app/layers.py:173` returns True on
`coverage == "all"`. `tests/test_layers.py:64-66` asserts exactly this.

docs/06 §6.3:339 already defines the rule form that *would* partition —
`field:<name>=<value>`, "the documents whose record field (a top-level
field, or `extra.<key>`) has that value" — and it is implemented
nowhere: `parse_layers` refuses any rule that is not `default` or
`archive:` (`app/layers.py:71-72`). **So the operator's ask is
expressible in the specification as written and is simply unbuilt.** That
is build work, not a spec change.

Two smaller things found in the same area, both real:

* a Codex carrying an unrecognised rule validates, then silently puts
  every document in the last layer (`codex.py:375` never checks the rule's
  form; `layers.py:198` returns False and `layer_index` falls through to
  the default). It should refuse.
* two layers may not name the same chunker (`codex.py:686`), so "one
  verbatim layer per producer" is refused at validation.

## 5. What actually delivers "re-embeddable on small changes" — DONE

Not layers at all. docs/06 §6.3 already specified it and nothing
implemented it:

> its vectors are carried over **by chunk hash, never by chunk id** …
> only a chunk whose text is new is embedded.

The only reuse in the codebase matched on (chunk_id AND byte-equal text),
and `seq` is a position — so inserting one paragraph shifted every later
id while changing no text, and the whole article went back to the
encoder. Measured on a 71-chunk article: an inserted paragraph gave 73
encoder calls where 3 were needed; a one-word edit gave 71 where 1 was
needed.

Built 2026-09-11 (`app/embeddings.py`, `tests/test_vector_carry_over.py`):
a rebuild now maps BLAKE2b-128 of each held chunk's text to its vector
and only genuinely new text reaches the encoder. This is the operator's
requirement, met, and it needed no spec change.

## 6. A live bug this uncovered, unrelated to the merge

**The wikipedia plugin's uid is not unique across languages.**
`discover()` rglobs every `*-pages-articles-multistream.xml.bz2` under the
corpus root (`app/plugins/wikipedia/source.py:110`) and computes a
per-dump language that it puts only in `DocRef.extra`, never in
`native_id` (`:155-160`). `documents.uid` is the primary key. Two dumps
therefore collide on every shared page id, and the collision is not an
error: discovery sees a different hash and runs
`UPDATE … rdf_done=0, fts_done=0, embed_done=0 WHERE uid=?`. The second
language overwrites the first and resets its stage flags; every scan
flip-flops the two.

wikivec spans eleven languages, so this blocks any wikipedia collection
over that corpus — but it is a defect on its own terms, today, for
anyone who mounts two dumps.

## Settled by the operator, 2026-09-11

1. **One collection, plugin `embedded`.** Not a preference — 09 §9.3 makes
   `plugin` the uid prefix and a collection has exactly one, so keeping
   `embedded:` is what holds all 91.2M chunk ids byte-identical.
2. **Two layers**, decided at creation because `layers` is inside
   `space_id`:
   * layer 0 — `verbatim`, rule `field:extra.cut=archive`. Upstash's
     paragraph cut; vectors reused as they are.
   * layer 1 — `cdc`, rule `default`. Everything new, and everything
     updated.
   An updated article's record is rebuilt from the new source and no
   longer carries the marker, so it falls to layer 1 and is cut by `cdc`
   — which is exactly "new embeddings use cdc". `content_hash` covers
   only title and sections (`app/pack/records.py:95`), so the marker
   changes no hash and no chunk.
3. **`record_line` 1 now, figures as a sister later.** `record_line` is
   inside `collection_id` but NOT inside `space_id`
   (`app/hold/codex.py:566-580`), and 02 §2.7's table gives a sister
   "vectors inherited when `space_id` matches". So the figures sister
   inherits every vector; `figures-in-the-corpus.md:269-277` reaches the
   same conclusion independently — same space, vectors admissible, "the
   work is a re-parse". Creating at `record_line` 2 is refused today
   anyway (`RECORD_LINE_BUILT = 1`).
4. **Figures take Commons' PNG rendition**, not the `.svg` — recorded
   with its reason in `pack-format.md`'s origin-form table, as invariant
   11 requires.

## The route, in order

1. Fix the wikipedia multi-language uid collision (§6). Required
   regardless of any merge.
2. Implement `field:<name>=<value>` layer rules (§4) — already specified,
   and the only thing that makes a second layer reachable. Refuse
   unrecognised rule forms while there.
3. `hold records build` over wikivec, read back with `packed` (§2) — one
   format, zero vectors touched.
4. Decide wikivec's layer list BEFORE writing its Codex (§3), and pass
   `--archives upstash-wikipedia-2024-06` on the sister.

§5 is done and stands on its own: it helps every collection, not only
this one.
