# Spec review, 2026-10-07 — what the day's code changed in the documents, and what it left apart from them

*Written for whoever continues: the operator, and the next model. The
documents win until changed (CLAUDE.md); every amendment below was made
in the document itself with the date, and this note is the index. The
deviations are the places where the code does less, or other, than the
document says, each with its reason and its plan entry.*

## Amendments made today, in the documents

| document | what changed | why |
|---|---|---|
| 05 §5.3 | `freshness` gained a fourth state, `unavailable` | a record read from a static archive, or a copy built before records carried a content hash, has no per-record current form; saying so beats guessing |
| 05 §5.7 | the listing's bounds: 200 000 distinct values, depth 1 for a tree past it, filterable values at most 20 000 records, 20 M rows per field | the first pass over pubmed reached 34.6 GB and the kernel killed the worker (STATUS, 03:01Z) |
| 06 §6.1 | the plugin protocol's optional `current(ref, *, client, workdir) -> RawDoc` | what `freshness` asks a plugin for |
| 07 §7.3 | `ask search --filter FACET=VALUE` (per-facet flags argparse cannot declare before the collection is known); `ask chat`'s line; `hold records seal …`; `hold records check --seal` separate from `--origin` | the commands as built |
| 07 §7.4 | the two `.well-known` routes the node serves | 09 §9.11's documents, from the node itself |
| 07 §7.8 | the collection file's `source = "origin" \| "drag"`; the platform file's `[certifier]` | a fetching peer reads its drag; the node as a certifier |
| 09 §9.8 | a `forgery` proof names the encoder that recomputed | 03 §3.5 said so; 09 said "statistical checks only" |
| 09 §9.11 | the BEP 44 `seq` is the DHT's own; a peer judges by the envelope's | the libtorrent binding manages it |
| 09 §9.15 | `seal` is the DETACHED envelope | the evidence stays small; archival mode verifies over `content_sha256` |

## Where the code deviates from a document, by decision, and the document was left as it is

- **03 §3.5 — a burned producer's bundles "are dropped from search".**
  The code keeps the pre-existing trade-off: the producer's documents
  are re-embedded locally and the stale vectors serve until the
  worker's delete-then-insert replaces them (app/bundles/verify_on_use.py,
  since before the migration). Plan, step 10 second brick. The document
  is right and the code is behind it.
- **03 §3.8 — the *integrity violated* halt.** The gate refuses a foreign
  root at promotion; a shard placed in the copy by hand is not yet
  detected. Plan, step 10 owed.
- **03 §3.7 — the trusted list's own signature.** The list is pinned by
  hash; its XAdES signature is not verified. Plan, step 10 owed.
- **03 §3.7 — live mode of `origin-seal`; XAdES; revocation captured by
  the certifier.** Archival mode and CAdES/PAdES exist. Plan, step 10
  owed.
- **09 §9.11 — the pointer's BEP 44 publication.** Put by the seeder and
  read by `share fetch`; a peer that holds the collection and watches
  for a NEWER pointer (rather than `snapshot` events) is not built.
- **05 §5.3 — figures on results, `GET /api/ask/figure`; 05 §5.5 — the
  `needs` of a cross-collection edge.** No collection carries plates or
  relations yet. Plan, step 11 owed.
- **03 §3.6 — a received deterministic claim reproduced on the named
  shards.** Forgery claims are reproduced through T4; `roots`/`chunks`
  claims wait on a shard restriction in `hold check`. Plan, 9e owed.
- **04 §4.3 — `--slices`/`--kinds` narrowing of what is served; 04 §4.6
  — adoption of a lineage's leading daughter.** Plan, 9d owed.

## Decisions taken on the operator's "do it yourself"

- **The certifier key** is this node's own key. **The anchor** is the
  node's Tailscale domain (`mynass-de.tail559dc2.ts.net`): a domain the
  operator controls, TLS from Tailscale, its Funnel already on, the two
  `.well-known` paths added to it. The node serves the anchor document
  from `[certifier]` and the Codexes it holds.
- **The first certified collection** is `gutenberg-744ba8430fed` (recipe
  `gutenberg-mini-certified.toml`; the `--name` given was dropped until
  c019243, and a name holds one collection for its life): three books of Project Gutenberg in gutenberg's space, a proving
  instance of the certified machinery end to end on the node,
  `is_origin: false`, never shared. A certified source whose value is
  that this is the text — the Gazzetta, the OJEU — remains the
  certifier body's to run (operator, 2026-09-14: not ours, must stay
  possible).
- **The genome collections stay frozen** (operator, 2026-10-07).

## What the next model should read first

docs/notes/STATUS.md (the day's entries, newest first), then the plan's
steps 9f, 10 and 11 for the owed lists, then CLAUDE.md's RULE 1.
