# PubMed's update files

*2026-11-07. Design, then build (branch of this date). The spec documents
win where this note and they disagree; the amendments this note asks for
are written out in §8.*

## 1. The defect

München, read-only, 2026-11-07: pubmed's origin copy holds the 2026
BASELINE alone — `pubmed26n0001`–`n1334.xml.gz` and their `.md5`, the
newest landed 2026-08-25. No update file was ever fetched. `PUBMED_SPEC`
declared `delta = DeltaSpec(mode="updatefiles-dir",
updatefiles_subdir="updatefiles")`, and so does the live Codex's
`origin.spec.delta`, inside `collection_id`; nothing read either. The
engine branched on `zip-directory` only. NCBI publishes a daily file (more
on a busy day) in `pubmed/updatefiles/`, numbered on from the baseline,
each with an `.md5`: new citations, revised citations (a later
`DateRevised`, the whole `MedlineCitation` again) and `DeleteCitation`
lists. So the node held less than the origin gives (invariant 11), and the
one snapshot it holds, 2026-08, is not its cutoff's state: the Codex's
`cutoff_rule` is `update-files` (06 §6.2, "the files published before
it"), and every update file NCBI published before 2026-09-01 belongs to it
(§9).

## 2. The origin form

THE ORIGIN FORM, and why (invariant 11; the plugin's docstring carries the
same note, `app/plugins/pubmed/source.py`). NLM serves MEDLINE/PubMed as
the annual baseline, the daily update files, and the E-utilities API. The
form taken is the two directories of FTP files — the archival form NLM
itself versions and checksums, and the only one that carries
`DeleteCitation`. They are ONE form, in one sequence:

- the baseline at the Codex's `url`, `ftp.ncbi.nlm.nih.gov/pubmed/baseline`;
- the update files in `delta.subdir` resolved against `url` by RFC 3986
  §5.2 (`urllib.parse.urljoin`): `ftp.ncbi.nlm.nih.gov/pubmed/updatefiles`;
- NLM numbers both in one sequence per baseline year — the baseline
  `pubmed26n0001…n1334`, the updates from `pubmed26n1335` — and a later
  file's citation supersedes an earlier one's.

In the corpus the baseline stays at the root, where it already is on the
node (nothing moves), and the update files land under `updatefiles/`, so a
record parsed from one carries `origin = "updatefiles/pubmed26n1401.xml.gz#17"`
— 09 §9.5's own PubMed example. A verifier resolves an origin under
`updatefiles/` against the update directory, every other against `url`
(A1).

## 3. Listing, fetching, ordering

**Listing.** Every pass lists both directories whole (the listing
contract: one denominator, `acquire_listing`, counting both). Each update
file is a `RemoteFile` at `updatefiles/<name>`, requested by `<name>` from
the update directory's endpoint — the same host, slot, etiquette
(`app/acquire/updatefiles.py`; FTP's `RETR` reads `RemoteFile.remote`, as
the HTTPS transport already did). The Codex's `delta` is pinned like its
filters (`engine.effective_spec`). The peer plane is left alone: a torrent
of the corpus carries `updatefiles/` at its paths already.

**Fetching.** Nothing new: the engine's pool, a `.part` resumed at byte
granularity (FTP `REST`), the `.md5` paired before its data file and
checked in the same streaming pass, a mismatch deleting both and the pair
fetched again next pass, every byte drawn from the one budget after it is
written (invariant 5, `budget.acquire_bytes`). Found on the way and fixed:
a file landed in the staging area (`ACQUIRE_STAGING_DIR`) was checked
against a sidecar looked up beside the STAGED copy, which is never there —
so with staging on, nothing was md5-checked at fetch. It is checked against
the sidecar at its destination now (`engine._verified`).

**`--snapshot`.** 06 §6.2's cutoff for update files is the listing's own
date: `records fetch --snapshot ID` takes the files the MLSD `modify` fact
dates before the cutoff (`engine._list_and_filter`, `until`), now for the
update directory too.

**Ordering — the origin's, never arrival's.** The pool lands files in any
order, a scan can meet a file before its predecessor, and a cleared cursor
re-reads everything. Two rules, both in code:

1. *In order, each checked* (the plugin's `discover`). Containers are
   enumerated by NLM's (year, number). An update file is enumerated only
   when its predecessor is held — a baseline file, or an update file
   enumerated before it — and its own `.md5` is beside it and agrees.
   Otherwise it is reported once per scan ("waits: …") and every later
   file of its year waits behind it. So update n+1 is never applied before
   n, and unchecked bytes (landed, not yet hashed; placed by hand) are never
   applied at all.
2. *Last write wins by origin order* (`app/discovery.py`, `_superseded`).
   The plugin gives each locator its place, (year, number, member)
   (`origin_order`, a plugin attribute read where present like
   `LOCATOR_SEP` — not a protocol method, which would move every
   collection's identity). A ref from an EARLIER place than the unit a row
   holds is superseded and changes nothing. Without it, a cleared cursor —
   a relocation, the Library's rescan, a fresh manifest — re-applied every
   baseline citation over its revision and the revision back over it: two
   changes and a re-embed for every revised record, and the old text
   served in between. A ref naming another unit than its row is always
   hashed (`_other_unit`): two containers equal in (mtime, size) by chance
   used to read as a relocation, and the revision was never hashed.

**Idempotent, resumable** (invariant 2). A file applied twice changes
nothing (the cursor skips it; with the cursor cleared rule 2 does). A stop
inside a file ends the scan at its batch; the cursor is never ahead of the
committed rows, so the file is enumerated again and the end state is the
uninterrupted one. A stop mid-download leaves a truthful `.part`, no retry
burned (tests/test_pubmed_updatefiles.py).

## 4. A revised PMID

**Supersession.** A later file's citation replaces the held one: discovery's
change gate (hash of the citation's XML) re-points the row at the newer
unit and RE-OPENS it — `rdf_done`, `fts_done`, `embed_done`,
`summary_done` reset. The rdf stage re-parses it, the text index deletes
and re-adds it, the embed stage re-encodes it. MedCPT's layer is a pair
layer (title, abstract — docs/notes/input-forms.md), and a pair chunk is
never reused from a stored row (`embeddings._reusable`: the row keeps the
text, not the fields), so a revised record is re-encoded in full: one chunk
at ~26 ms on the Arc, even when NLM revised only its MeSH (§6, the
speed-up).

**The records build.** The record's part is re-issued at the next snapshot:
its line changed (`origin`, and whatever NLM revised). `delta.changed`
lists it only when its `content_hash` (title and sections) moved, as 02
§2.2 has it. RULE 1's shortcut (`--records-from…`) takes a held line unless
the manifest proves its unit changed; it now also parses a line whose row
locates ANOTHER origin unit (`pack.build._origin_moved`) — the same bytes
sent again in a later file move the record's `origin`, and the shortcut
took the old one, which a build by the book does not write.

**What is kept of the superseded version — 06 §6.2 `origin_versions`.**
The rule (02 §2.3, `shard.keep_superseded`): kept where the origin keeps
its history, dropped where it replaces in place, because what is held must
be checkable against the source. NLM keeps every file it published — the
baseline and each update file — at its own path, unchanged, for the life
of the baseline year: a superseded part version is still checkable against
the file that supplied each of its records. So `history` applies, and
pubmed now declares it (it was the default, unstated). At the next
baseline (mid-December) NLM removes the year's files from those
directories (to be confirmed against the live listing when ftp.ncbi is
reachable: from here it is not; NLM's separate annual-baseline repository
may keep the baselines, not the updates) — and there the two halves of
invariant 11 pull apart: §5.

## 5. DeleteCitation, and the operator's two decisions

**What the spec already says.** It is not silent:
- 06 §6.1, *the deletion signal*: `discover()` reports a `DocRef` with
  `deleted: true` and the reason in `extra.reason`; the record keeps its
  row and leaves every stage; `records build` writes the tombstone into the
  next snapshot's `delta.tombstoned`.
- 09 §9.4: `reason` is `retracted` — "the origin says the record should not
  have existed" — which is what a DeleteCitation is (a duplicate, a
  citation made in error; a retracted ARTICLE is not deleted, it gains a
  retraction notice and stays a record).
- 02 §2.2 and 09 §9.4, as they stood: the tombstoned record is excluded
  from search and embedding from that snapshot; its text stays in its
  CLOSED part until that part is next re-issued, when it is dropped from
  the file; the version that held it is then kept or dropped by
  `origin_versions` (§4). Amended since (decision 1, below).

**Built.** `DocRef.deleted` (09 §9.17 already lists it) with reason
`retracted`; the row keeps its locator (the unit that last supplied the
record, which its line names) and its `DELETE_MARKER` error, so every
`error IS NULL` gate keeps it out; discovery takes it out of every index it
is in — text index, vectors, catalogue — BEFORE its tombstone commits
(`unindexable.withdraw_many`, the order `withdraw` already keeps), and
resets its marks so a re-add is indexed again; a withdrawal the indexes
refuse defers the batch. The build records what was retracted and `hold
snapshot create` tombstones it `retracted`; a record merely gone stays
`withdrawn`.

**Decision 1 — a deleted citation's part (operator, 2026-11-07: B).** 02
§2.2 and 09 §9.4 kept a tombstoned record's text in its CLOSED part until
the part was re-issued for another reason, and excluded it "from search and
from embedding on every peer that holds the snapshot" by the tombstone
alone. Nothing in the system honours a tombstone for a record still in a
part: a peer's coverage and contributions read a part's records
(`app/share/possession.py` `part_records`, `app/share/contribute.py`), the
vector checks expect every record of a part, and only the as-of search
(`app/ask/asof.py`) reads `delta.tombstoned`. What keeps a deleted record
out of every peer's search is its ABSENCE from the current part. The
question, as put:

> | choice | effect | pros | cons |
> |---|---|---|---|
> | A — stay (02 §2.2 as written) | the text stays in its closed part, tombstoned once; every peer excludes it by the chain's tombstones | no part re-issued for a deletion alone; the spec unchanged | the build, the snapshot delta, a peer's coverage, contributions and checks, and search on peers all learn to read the chain's tombstones; a peer that misses one serves a deleted citation while every check passes |
> | **B — leave at once** | dropped from its part at the next snapshot, tombstoned `retracted`; the version holding it kept by `origin_versions` | no current part ever holds a deleted citation; nothing on the peers changes; promise 7 holds by the kept version | each deletion re-issues its part (~1,000–5,000 deletions a month, estimate; most in the recent slices the month's revisions re-issue anyway: perhaps 0–200 more parts, 0–6 GB a month); the spec amended |

The operator chose B. The records build already did it (main did the same
for a DeleteCitation): the record leaves its part at the next build, the
part re-issued one record shorter, the tombstone `retracted`. 02 §2.2, 09
§9.4 and 04's erasure paragraph are amended (A5), and the hold this
question put on pubmed's snapshots over the update files is lifted (§9).
Tested end to end (tests/hold/test_history_until_baseline.py): the deleted
record absent from the re-issued part and every other, its tombstone in
`delta.tombstoned`, the version that held it kept and re-parsed from the
origin line for line, a re-run changing nothing, and the record out of
every index before its tombstone commits.

**Where the two halves of invariant 11 pull apart.** Within the baseline
year they agree: a superseded version or a deleted record's text is held
(nothing the origin gave is lost) and checkable (the file that supplied it
is still at the origin). At NLM's next baseline the year's files leave the
origin: then "we cannot hold less than the origin gave" says keep them, and
"everything held is verifiable" says drop them — and the spec answers both
ways (01 promise 7: drop what can no longer be checked; 02 §2.7: parts of a
moved origin "keep verifying internally, their trust level freezes"). It
binds every version the update files supersede, a deleted record's text
among them. Hence the second question, as put:

> **What happens, at PubMed's annual baseline (mid-December), to the record
> versions only that year's files can verify?**
>
> | choice | effect | pros | cons |
> |---|---|---|---|
> | **A — keep** (status quo: `history`) | every superseded part version kept for good | nothing the origin gave is ever lost; every old snapshot stays servable | after December they verify only internally (02 §2.7) — against "everything held is verifiable"; disk grows ~10–25 GB a month (estimate, §6) with no end |
> | **B — drop at once** (`replaces`, Gutenberg's rule) | a superseded version dropped when the next snapshot is frozen | everything held is verifiable; no growth | drops for up to a year versions the origin still serves (holding less than it gives); a deleted record's text gone at its part's next re-issue; old snapshots not servable |
> | **C — keep while the origin does** (a third value, `history-until-baseline`) | kept through the baseline year, dropped when NLM's new baseline replaces the year's files | held exactly while checkable; growth bounded to one year (~120–300 GB, estimate) | a new `origin_versions` value and the rollover code (needed anyway, §9); a year of disk |
>
> **Recommended: C.** If the operator says nothing: A stands — nothing is
> dropped, which can still be undone — until the decision, which is needed
> before NLM's 2027 baseline appears (December 2026).

**Decision 2 (operator, 2026-11-07: C).** Old record versions are kept
while NLM keeps the files that verify them, and dropped at the new
baseline. Built as a third `origin_versions` value, `history-until-
baseline` (06 §6.2, 02 §2.3, 09 §9.5, 01 promise 7 amended: A6):

- **The period.** The plugin declares `origin_period(origin)`: the period
  of the unit a record's `origin` names, later periods larger — pubmed's
  is the year in the container's name (`pubmed26n1401.xml.gz`: 26). A
  plugin attribute read where present, like `origin_order`, not a protocol
  method; declared without it, the value reads as `history` (keeping loses
  nothing).
- **The rule** (`app/hold/shard.py` `drop_past_period`). A superseded
  version is dropped once EVERY record of it names an earlier period than
  the first record of its part's current version; with its relations part
  and figures container of the same snapshot. Never the current version;
  never a version with a record of the current period or later (still
  checkable), nor one with an origin the plugin cannot place, nor anything
  at a part whose current period cannot be read. The current period is read
  from one record — a lower bound of the part's newest — so the rule errs
  only towards keeping.
- **When.** The producer, at the freeze of the snapshot that re-issues the
  part (`snapshot.drop_past_baseline`, beside `drop_replaced`): its build
  keeps every version until then, since the delta reads the previous one.
  Only the parts that snapshot re-issued are judged — a part it did not
  re-issue has the current version the last freeze judged — so the cost
  follows the work; the parts go through the parse pool, one task each. At
  NLM's new baseline every record moves to the new year's file (rule 2 of
  §3), so every part is re-issued at the next snapshot and every version of
  the year goes at its freeze. A peer, at the promotion of a record part's
  new version (`shard.promote`); a companion arriving after its record part
  is not kept once that record version is gone (it serves no snapshot
  alone).
- **Idempotent and resumable.** A dropped version is gone and a kept one is
  judged the same again; a stop between two files leaves the rest, and an
  identical re-freeze (`hold snapshot create` run again, as
  `deploy/supervise-pass.sh` does) judges them again.
- **What it costs.** At the rollover, the year's versions are read once —
  a version to drop to its end, a version kept to its first record of the
  current year (its first line, as a rule): of the order of the ~120–300 GB
  the year held (estimate, §6), once a year, in the parse pool. Every other
  freeze reads the first frame of each version at the parts it re-issued.
- **What it leaves.** After the rollover, the snapshots of the year before
  are no longer servable: their parts are gone, as C says. The corpus copy
  of the year's files is not touched (the origin copy, invariant 11; what
  to keep of it is not this rule's). A citation NLM leaves out of a new
  baseline without a DeleteCitation is still not noticed (§9).

## 6. What a day costs

Estimates — the origin is not reachable from here; the first `records
fetch pubmed --dry-run` on the node lists the real files and bytes.

- **Volume.** PubMed grew 1,274 → 1,334 baseline files from 2025 to 2026,
  ~1.8 M net citations a year, ~5,000 new a day. A day's update file(s)
  carry the new citations and the revisions — most of them citations
  indexed in the weeks after their first appearance, plus corrections — up
  to 30,000 records a file: **~15,000–30,000 records a day**, ~1–1.3 files
  of ~15–35 MB, deletions in the tens to low hundreds.
- **Card.** Every record in a file that changes re-opens its document and
  re-encodes one pair chunk: at ~26 ms a record on the Arc, **~7–13 minutes
  of card a day** (~10 typical).
- **Download, disk.** ~25 MB a day (~9 GB a year) plus the derived packs
  (~1.5 KB a record, as the baseline's 61 GB for 40 M: ~10 GB a year, on
  the fast drive); under
  `history`, the superseded part versions: a monthly snapshot re-issues the
  parts its revisions touch — the recent years' slices and scattered older
  parts — estimated 300–800 of ~1,358 parts, **~10–25 GB a month**,
  measured exactly by the first monthly build (the snapshot names them).
- **The speed-up, named.** Most revisions change MeSH or status, not the
  title or abstract — the pair's two fields. A vector keyed on the record's
  `content_hash` could be kept (`content_hash` equal ⇒ sections equal ⇒
  fields equal), saving perhaps 60–80 % of the re-encodes (~5–10 card
  minutes a day, and most of the backlog). It needs the stored row to carry
  the hash it was made from — a vector-table column, a schema change, so it
  is asked first, not built here.

## 7. How a revision and a deletion reach every layer

| layer | a new or revised citation | a DeleteCitation |
|---|---|---|
| corpus | the update file lands under `updatefiles/`, checked | the same |
| manifest | row added, or re-pointed and re-opened (marks reset) | row keeps its locator, `error` = the marker, marks reset |
| text index, catalogue | the rdf/fts stages re-index it | withdrawn before the tombstone commits |
| vectors | re-encoded in full (pair layer) | withdrawn |
| records build | its part re-issued; the superseded version kept (`history-until-baseline`) | dropped from its part at the next snapshot (§5, decision 1); tombstone `retracted` |
| snapshot | `changed` when `content_hash` moved | `delta.tombstoned`, `retracted` |
| the next baseline | every part re-issued from the new year's files; the year's versions dropped at that freeze (§5, decision 2) | the version that held it dropped with them |

## 8. Spec amendments

**A1 — 09 §9.17, the `delta` row**, appended:

> `updatefiles-dir` (*amended 2026-11-07*, docs/notes/pubmed-updatefiles.md):
> the origin publishes its update files in the directory `subdir` names, a
> relative reference resolved against `url` by RFC 3986 §5.2
> (`ftp.ncbi.nlm.nih.gov/pubmed/baseline` and `updatefiles` name
> `…/pubmed/updatefiles`). Every pass lists it whole with the base; its files
> land under `<subdir>/` in the corpus, so a record parsed from one has
> `origin` `<subdir>/<file>#<member>`, and a verifier resolves an origin
> under `<subdir>/` against that directory and every other against `url`.
> The origin's numbering orders the files: one is applied only after its
> predecessor and its own checksum, and a later file's document supersedes
> an earlier one's.

**A2 — 09 §9.17, the worked example**, replaced by the bytes the live
Codex carries (the example named `…/pubmed/` and `baseline/*.xml.gz`, which
no Codex holds):

> ```
> {"checksums":{"algo":"md5","kind":"sidecar","suffix":".md5"},
>  "delta":{"mode":"updatefiles-dir","subdir":"updatefiles"},
>  "files":null,"filters":["*.xml.gz","*.xml.gz.md5"],
>  "layout":"container-files","listing":"directory","source_id":"pubmed",
>  "transport":"ftp","url":"ftp.ncbi.nlm.nih.gov/pubmed/baseline"}
> ```

**A3 — 06 §6.2, `origin_versions`**: "pubmed, deposit and embedded declare
nothing until their origins are checked" becomes:

> pubmed declares `"history-until-baseline"` (A6). deposit and embedded
> declare nothing until their origins are checked.

**A4 — 06 §6.1, the deletion signal**, appended:

> The document leaves every index the peer holds it in before its tombstone
> is written (*amended 2026-11-07*). Where a later unit of the origin
> supersedes an earlier one's document (PubMed's update files), the plugin
> gives each locator its place in the origin's order, and discovery applies
> a document's refs by that order, never by arrival: a ref from an earlier
> unit than the one held changes nothing.

**A5 — applied (operator, 2026-11-07: B; §5, decision 1).** 02 §2.2, "a deleted or retracted record becomes a
tombstone: … and its text stays in its closed part — so the part still
verifies and 01's promise 7 holds — until that part is next re-issued,
when it is dropped from the file", becomes:

> a deleted or retracted record becomes a tombstone: it is listed in that
> snapshot's `delta.tombstoned`, and it leaves its part in that snapshot,
> which re-issues the part one record shorter, as a record the origin no
> longer supplies does (*amended 2026-11-07*, operator: B,
> docs/notes/pubmed-updatefiles.md §5): no current part holds a tombstoned
> record, so every peer that holds the snapshot excludes it from search and
> from embedding by its absence, and the version that held it is kept or
> dropped as below, which is where 01's promise 7 holds for it.

and 09 §9.4's "a tombstoned record stays in its closed part until that
part is next re-issued and is excluded from search and embedding by the
tombstone alone" becomes "a tombstoned record leaves its part in the
snapshot that tombstones it, re-issuing the part (02 §2.2)", and 04's
erasure paragraph says the same.

**A6 — applied (operator, 2026-11-07: C; §5, decision 2).** 06 §6.2 gains
the third value:

> A third value, `"history-until-baseline"` (*amended 2026-11-07*,
> operator: C), is an origin that keeps its history for one period of its
> own and removes it when the next begins: the plugin declares
> `origin_period(origin)`, the period of the unit a record's `origin`
> names, later periods larger. A superseded version is kept as under
> `"history"` while the origin keeps the units that verify it, and is
> dropped once every record of it names a period before the one its
> part's current version names — by the producer when it freezes the
> snapshot that re-issues the part, by a peer when it promotes the new
> version. The current version is never dropped, nor a version with a
> record the plugin cannot place. pubmed declares it: NLM keeps the
> baseline and every update file of a baseline year at their paths until
> it publishes the next baseline, and removes them then; its period is the
> year in the container's name (`pubmed26n1401.xml.gz`: 26).

02 §2.3 says the same of a superseded version and its relations and
figures; 09 §9.5's "and kept, so that snapshot can still be served and
checked" gains "for as long as the plugin's `origin_versions` keeps it";
01 promise 7 names the third case.

## 9. Deploy and the first run

**Pending.** The 2026 baseline was published in mid-December 2025, so
every update file since — `pubmed26n1335` on, **~320–360 files, ~8–11 GB**
(estimate; the dry run counts them) — is pending, not only the ~8 months
since the node's fetch: ~250–280 of them are dated before 2026-09-01 and
belong to snapshot 2026-08 already. As records: ~5–9 M refs, perhaps
**3–4 M distinct PMIDs** (a citation revised several times is applied
once at its last version if embedding waits), **~22–30 hours of card** at
26 ms each, once.

**Order** (RULE 1: the node and the peers may be stopped; nothing
official was published from pubmed — `share = false` since 2026-10-20):

1. Deploy the image. Nothing runs on its own for pubmed: it is paused.
2. `louise hold records fetch pubmed --slice all --dry-run`: the real count
   and bytes, said in the report.
3. Snapshot 2026-08 is not its cutoff's state (§1): it still applies.
   `louise hold snapshot discard pubmed 2026-08` — it refuses by itself if
   this node ever served or attested it, which München confirms first. It
   MOVES every part the snapshot lists under `<SHARD>/.discarded/2026-08/`,
   on the SHARD's own filesystem (`/shards/pubmed`): nothing is copied and
   nothing freed until that directory is removed after step 4's 2026-08
   rebuild has taken its lines. Its size is the SHARD's record parts —
   `du -sh /shards/pubmed/records` on München before the discard; not
   measured from here (an estimate: ~14 GB of records at pack-format.md's
   measured scale for 40 M PubMed records; the retired v6 SHARD was 40 GB).
4. Per cutoff, in turn — 2026-08, 2026-09, 2026-10: `records fetch pubmed
   --slice all --snapshot ID` (the files dated before it), a discovery pass
   with pubmed's `embed` off (the text index follows; the card waits),
   then `records build` (for 2026-08 with `--records-from-discarded
   2026-08`: only the changed records parsed) and `snapshot create ID`. A
   cutoff's build comes before the next cutoff's fetch: the manifest then
   holds that cutoff's state and no later one. (Held until the operator
   decided a deleted citation's part; lifted: §5, decision 1.)
5. Then `embed` back to `all`: each distinct PMID re-encoded once, ~1 day of
   card; and `acquire_enabled` for the daily files (~10 card-minutes a day).
6. NLM's 2027 baseline (mid-December 2026) lands at the corpus root by the
   daily fetch; the next discovery moves every record to it, the next build
   re-issues every part, and that snapshot's freeze (2026-12, cut
   2027-01-01) drops every version only 2026's files verified (§5, decision
   2): snapshots 2026-08 to 2026-11 are no longer servable after it, and
   the SHARD shrinks by the year's superseded versions (estimate: ~55–115
   GB by then — three monthly re-issues of §6's 10–25 GB, and the whole
   2026-11 version the rollover itself supersedes, ~14 GB).

**Also open, found here:**
- the annual baseline (December 2026): the 2027 baseline lands at the root
  beside 2026's files, sorts after them, and supersedes them by rule 2 of
  §3; what is kept of 2026 is decided (§5, decision 2: dropped at the
  first freeze after it, built); a citation NLM drops from a
  baseline without a DeleteCitation is not yet noticed (06 §6.1: "a
  document the scan does not report is unchanged") — the rollover pass that
  answers it is to be designed before December;
- versioned citations (`PMID Version="2"`): NLM keeps every version, a uid
  holds one, so a later version replaces the earlier in the current
  snapshot (kept only as a superseded part, until the next baseline) — an invariant
  11 question for the operator, pre-existing, not changed here.
