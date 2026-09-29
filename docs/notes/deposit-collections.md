# Deposit collections — Louise as LocalAGI's knowledge base

**Status: PROPOSAL, for the operator's decision. Nothing built.**
Written 2026-09-29 at the operator's request: *make Louise able to offer
the connectivity LocalAGI expects of a knowledge base, so it can take
that place in LocalAGI's ecosystem.* The reference project is
**LocalAGI** (`mudler/LocalAGI`, read at `7e0947d`, 2026-09-27) —
specifically the HTTP client it uses when a knowledge base is external,
`pkg/localrag/client.go`. LocalRecall, the server that client was
written against, is deliberately not the reference: what Louise must
satisfy is what LocalAGI sends and reads, nothing more.

Every amendment below names the section of 01–09 it changes. Where a
note and a document disagree the document wins, so none of this binds
until the documents are changed.

## 1. What LocalAGI needs

LocalAGI keeps its knowledge base in-process by default. It talks to an
external one over HTTP when `LOCALAGI_LOCALRAG_URL` is set for the pool,
or `local_rag_url` / `local_rag_api_key` for one agent
(`cmd/agent_run.go`, `core/state/config.go`). So an operator points one
agent, or all of them, at a Louise node; nothing else changes on
LocalAGI's side.

Two uses, both of which **write**:

- **Agent memory.** Each agent gets a collection named after itself,
  lower-cased (`NewWrappedClient`). `Store(text)` writes the text to a
  file named `YYYY-MM-DD-HH-MM-SS-<md5>.txt` and uploads it. `Search`
  asks for the top *n* and formats each result as
  `Content (Metadata)` for the model. `Count` is `len(ListEntries)`.
- **Compaction** (`core/state/compaction.go`). Periodically it lists
  the entries, **parses the date out of each entry key**, groups them
  by period, reads their content, stores one summary, and deletes the
  originals. So an entry key must be the filename as uploaded.

The Web UI's *Knowledge base* page drives the same client over every
route (`webui/collections_backend_http.go`).

### The contract — every call the client makes

Base: `<url>/api/collections`. Auth: `Authorization: Bearer <key>` when
a key is set. Every JSON answer is wrapped as
`{success, message?, data?, error?: {code, message, details?}}`, and an
error is read from `error.message` / `error.details`.

| call | request | success | `data` read by the client |
|---|---|---|---|
| CreateCollection | `POST /` `{name}` | **201** | — (error ignored by agents: called on every agent start) |
| ListCollections | `GET /` | 200 | `{collections: [string]}` |
| ListEntries | `GET /{c}/entries` | 200 | `{keys: [string]}` (falls back to `entries`) |
| GetEntryContent | `GET /{c}/entries/{key}` (path-escaped) | 200 | `{content, chunk_count}` — the full text, no overlap |
| DeleteEntry | `DELETE /{c}/entry/delete` `{entry}` | 200 | `{remaining_entries: [string]}` |
| Search | `POST /{c}/search` `{query, max_results}` | 200 | `{results: [{ID, Metadata: {string: string}, Content, Similarity}]}` |
| Reset | `POST /{c}/reset` | 200 | — |
| Store (upload) | `POST /{c}/upload`, multipart field `file` | 200 | **unwrapped** `{status, filename, key}`; an unparseable body still counts as success |
| AddSource | `POST /{c}/sources` `{url, update_interval}` (minutes, ≥ 1) | 200 | — |
| RemoveSource | `DELETE /{c}/sources` `{url}` | 200 | — |
| ListSources | `GET /{c}/sources` | 200 | `{sources: [{url, update_interval, last_update}]}` |

`Search`'s result fields carry no JSON tags on LocalAGI's side, so
Go's case-insensitive decoding accepts `id`, `metadata`, `content`,
`similarity`. The embedding field is never read.

## 2. What the specification already provides

More than expected — the hard parts are already rules:

- **`erasure: erasable` + `privacy: never-share`** (02 §2.1, 04 §4.4,
  06 §6.1, enforced in `app/hold/codex.py`). Agent memory is exactly
  this: content that must be deletable without trace and must never
  leave the node.
- **A protocol alias is a precedent** (07 §7.6):
  `/v1/chat/completions` is kept "for clients that speak that
  protocol". LocalAGI's routes are the second such protocol.
- **Collections in one space fuse as one list** (05 §5.1). A deposit
  collection in the same space as a curated one is searched beside it
  with no new machinery (see A3 for when that holds).
- **The calibration set is corpus-independent** (03 §3.3): it measures
  an encoder and engine, not a collection, so a new collection on an
  already-calibrated encoder needs no new calibration.

What is missing is a source whose origin is **the caller**, and a
definition of what "no trace" means in bytes.

## 3. The amendments

### A1 — Vocabulary: *deposit* (01, vocabulary table)

| term | meaning |
|---|---|
| deposit | a document a client hands to the node over HTTP, kept byte-exact as its own origin |
| deposit collection | a collection whose plugin is `deposit`: created by a client, written by a client, `never-share` and `erasable` by construction |

### A2 — The `deposit` plugin (06, new §6.7)

A platform plugin, reviewed like any other, whose origin is the
collection's own `origin/` directory (07 §7.8) — the deposit store.
Like every `origin/`, it is never a member; unlike most, nothing else
holds a copy of it.

| declaration | value | why |
|---|---|---|
| `license_class` | green | nothing is fetched from anyone; see D4 |
| `privacy_class` | never-share | not tightenable further, and may not be loosened (06 §6.1) |
| `erasure` | erasable | a deposit may be withdrawn and must leave no trace (A4) |
| `modality` | text | v1 accepts text only (D5) |
| `mutability` | immutable | a deposit is replaced whole, never edited in place |
| `native_id_order` | numeric | A5 |
| `corpus_provider` / `corpus_attribution` | "the depositor" / "Deposited by a client of this node." | declared, like every plugin's, green included |

- `discover` lists the store; `fetch` reads the deposited bytes;
  `parse` decodes UTF-8 and cuts sections on Markdown headings, one
  untitled section otherwise. Bytes that are not valid UTF-8 are
  refused at upload (415), never stored and never guessed at.
- `origin_hash` is BLAKE2b-128 of the bytes exactly as deposited; the
  record's `origin` is the store path relative to its base. So
  `records-vs-origin` re-parses the kept bytes and must reproduce the
  record — invariant 11 holds, because the origin is kept for as long
  as the record exists, and both go together (A4).
- The **deposit key** (the filename the client sent) is kept as
  `extra.key`; the upload time as `extra.deposited_at`. The latter is
  kind (c) of promise 9 and could be dropped; it is kept because
  compaction clients reason about time and it costs one field.

### A3 — The Codex of a deposit collection (02 §2.1)

Created by `hold collection create deposit` (A6), with every field
present as usual. What differs:

- **`origin.spec = {kind: "deposit", nonce: "<128 random bits, hex>"}`**,
  `form: "deposit: the bytes as the client sent them"`, `unit: "file"`,
  `clock: "UTC"`, `cutoff_rule: "the deposits held at the instant"`,
  `jurisdiction: null`.
  **The nonce is load-bearing.** `collection_id` hashes the Codex
  without `descriptive`, and the name lives in `descriptive.title`. Two
  agents' collections created from the same template would otherwise
  be *the same collection* — same id, same drag path. A deposit is not
  reproducible from a public origin, so no two deposit collections are
  ever the same one; the nonce says so in the identity.
- **`space`** is copied whole — encoder, engine, calibration, envelope,
  tolerance — by `create`'s existing `--space-from CODEX`, defaulting to
  `platform.toml` `[deposit] space_from` when the API creates. That
  collection's encoder has already passed the gate, and the calibration
  set does not depend on the corpus (03 §3.3), so **the minute `create`
  spends embedding the calibration set is skipped: the bytes are
  identical** (CLAUDE.md RULE 1: the default, reported, not asked).
  `space_from` must name a `modality: text` collection; `create`
  refuses anything else.
- **`layers`** are the deposit plugin's default: one layer, `fixed`.
  `space_id` hashes layers, so a deposit collection is in the same
  space as `space_from` only if that collection is also one `fixed`
  layer; otherwise it is its own space on the same encoder, and 05
  §5.1 fuses the two by RRF — correct either way, since both answer
  the same question.
- **`update`** — the schema (09 §9.3) requires a cadence; deposit
  collections take `monthly`, day 1, grace 0. The snapshots stay local
  (A4).
- `credits` holds the single `corpus` entry; `validation` samples like
  any collection; `publication: "open"`, `certifier: null`.

### A4 — What "no trace" means for an erasable collection (02 §2.2–2.3, 04 §4.4)

Today "erasable" is a definition with one enforcement: it cannot be
shared. The second half — *must leave no trace* — is not made concrete
anywhere, and three of the platform's own stores keep deleted data by
design. Proposed rule: **erasing a record removes every byte derived
from it, on this node, before the call returns success**:

1. the deposited origin bytes;
2. the record, its manifest rows and its chunks;
3. its text-index entries in `index/` — tantivy marks a deleted
   document and keeps its postings until the segment is merged, so an
   erase merges the segments it touched;
4. its vectors **including in Lance's older table versions** — Lance
   keeps deleted rows until `cleanup_old_versions` runs, so an erase
   runs it on that table (bounded: a deposit table is small);
5. every record-shard version and snapshot that contains it. **An
   erasable collection keeps only its current snapshot**: 02 §2.3's
   "a superseded version is renamed and kept" and 02 §2.2's
   `delta.tombstoned` do not apply to it, and an erased uid is named in
   no delta. Promise 7 is about what is *published*; a never-share
   collection publishes nothing, so there is nothing it preserves here.
6. SQLite: the build has `SECURE_DELETE` on (`app/db.py`), so freed
   pages are zeroed; the deposit manifests must never turn it off the
   way the catalog drop does.

`Reset` is erasure of every record, followed by the same cleanup.
Enforced by **test**: deposit, embed, erase, then assert the bytes
appear in no file under the collection's state and drag directories.

### A5 — Entry identity (02 §2.3)

- `native_id` is a **node-wide monotonic deposit number**, so it is
  unique within the plugin as 02 §2.3 requires, and later deposits sort
  after earlier ones — the shard-order rule of 02 §2.2, which a
  client-chosen filename cannot promise.
- The client sees only the **key** (`extra.key`): unique within the
  collection. Depositing an existing key **erases the old record (A4)
  and stores a new one** — a replace, never an edit.
- Keys are 1–255 bytes of UTF-8, no `/`, no NUL, no leading dot.

### A6 — Creating a collection from an API (07 §7.3)

`hold collection create deposit --name <name> [--space-from CODEX]` —
the existing verb with the `deposit` plugin; `--name` is the one new
flag, and the recipe is the plugin's defaults, so none is needed. It
writes `collections/<name>.toml` (desired state lives in files,
invariant 10 — never in the DB, even when an API created it), the
Codex of A3, and the empty `origin/`. The name is
`[a-z0-9][a-z0-9_-]{0,63}` (LocalAGI lower-cases agent names already).
A name held by any collection, deposit or not, is refused with 409.
`hold collection forget` of a deposit collection is A4's erasure of
everything, then the directory.

### A7 — The LocalAGI surface (07 §7.4, §7.6)

A second protocol alias, beside `/v1/chat/completions`: the routes of
§1, each one a thin translation onto the tree, answered in LocalAGI's
envelope.

| route | is |
|---|---|
| `POST /api/collections` | `hold collection create deposit --name` |
| `GET /api/collections` | `hold collection list` — deposit collections, plus curated ones (D2) |
| `GET …/{c}/entries` | the collection's keys |
| `GET …/{c}/entries/{key}` | the record's text, joined; `chunk_count` from its chunks |
| `DELETE …/{c}/entry/delete` | erase (A4), then the remaining keys |
| `POST …/{c}/search` | `ask search` scoped to `{c}`, `limit = max_results` |
| `POST …/{c}/reset` | erase all (A4) |
| `POST …/{c}/upload` | deposit (A2, A5, A8) |
| `…/{c}/sources` | D3 |

- **Search results.** `ID` = uid; `Content` = the chunk text;
  `Metadata` = `{collection, uid, key, title, origin}` as strings for a
  deposit, and the record's citation fields for a curated collection;
  `Similarity` = the vector leg's cosine when the chunk came from it,
  `0` for an FTS-only hit. LocalAGI does not read it; it is written so
  that a client that does is not lied to.
- **Writes to a curated collection** answer 409 "not a deposit
  collection". Curated collections are read-only here, always.
- **Status codes** follow the client, not 07 §7.4's mapping: 201 on
  create, 200 on success, otherwise 07's codes with the error envelope.
  This is the one place the mapping differs, and it is written here so
  nobody extends it by analogy.
- **Auth.** A new token, `DEPOSIT_TOKEN_FILE` (a path, invariant 8),
  is required on **every** route of this surface, reads included,
  and grants nothing else. It is not the admin token: an agent that
  can write its memory must not be able to pause the node. And it
  closes a gap: `ask` routes are LAN-open (07 §7.4), so **deposit
  collections are excluded from the open `ask` routes and from the web
  Ask page**; they are searched only with the deposit token or the
  admin token. Never-share is not only about peers — a guest on the LAN
  is not the depositor either.

### A8 — When a deposit becomes searchable

LocalAGI stores a memory and may search for it on the next turn. The
upload request therefore does, in one `BEGIN IMMEDIATE` transaction
after the bytes are fsynced: record, chunks, FTS postings, done-mark —
all cheap, all local. **Vectors are the worker's**, in its next round,
through the ordinary vectors stage and RAGE. Until then the deposit is
found by the FTS leg and not by the vector leg; the answer says nothing
false, only less.

The encoder call stays out of the request: an HTTP handler that waits
on RAGE without a bound is the hang of amendment 51, and one with a
bound turns an agent's memory write into a timeout whenever the GPU is
busy. This touches what runs when, so **`scheduling.md` §6's checklist
applies before it is built**, with `dev schedule-report` numbers: a
stage whose work is "one small deposit" must not win rounds from
PubMed's backlog, nor starve behind it.

## 4. Deliberately not proposed

- **No sharing of deposits, ever.** Not as an option, not later:
  erasable ⇒ never-share is arithmetic (04 §4.4).
- **No PDF, DOCX or other binary formats in v1.** Each needs a pinned
  extraction library in the image — a new dependency, the operator's
  call (D5).
- **No compatibility beyond what LocalAGI's client sends.** LocalRecall's
  other routes (raw file download, its own UI) are not a target.
- **No change to any curated collection.** They gain a read-only view,
  nothing else.

## 5. Decisions for the operator

**D1, D2 and D4 accepted by the operator, 2026-09-29.** D3, D5 and D6
open.

- **D1 — Adopt A1–A8 as the amendment to 01, 02, 04, 06 and 07?**
  09 needs no change: nothing here is hashed, signed or moved between
  peers, except the Codex, whose schema is unchanged.
- **D2 — Do curated collections appear to LocalAGI?** *Recommended:
  yes, read-only.* It is what makes the integration worth more than
  LocalRecall: an agent searches Normattiva beside its own memory. The
  cost: LocalAGI's UI will offer "upload" and "reset" buttons on them,
  which answer 409.
- **D3 — `sources` routes.** *Recommended: answer 501 in v1.* A URL
  fetched on a timer is acquisition — licence gates, the transfer
  budget (invariant 5), origin etiquette. Doing it properly is a
  plugin of its own; doing it improperly is the bypass invariant 9
  forbids. LocalAGI's UI shows the error; agents never call these.
- **D4 — Licence class of a deposit.** *Recommended: green*, with
  `LICENSE-TERMS` stating that deposited content is the depositor's and
  never leaves the node. Yellow or red would demand an acknowledgement
  per collection that the depositor would be giving to themselves; red
  would make records pointers, which has no meaning here.
- **D5 — Which formats in v1.** *Recommended: UTF-8 text and Markdown
  only* — all LocalAGI's agent memory produces. PDF on request, with
  the library chosen then.
- **D6 — `space_from`.** Which collection's encoder deposits use.
  *Recommended:* the text collection most searched beside agent memory
  (for Italian law, Normattiva's), so D2's fused search compares like
  with like.

## 6. How it would be proved done

- `tests/deposit/test_localagi_contract.py`: every request of §1,
  byte-for-byte as `pkg/localrag/client.go` at `7e0947d` sends it,
  replayed against the app; every response decoded the way that client
  decodes it (status code, envelope, the unwrapped upload body).
- `tests/deposit/test_erasure.py`: A4's no-trace assertion, including
  Lance's old versions, unmerged index segments and superseded shard
  files.
- `tests/deposit/test_identity.py`: two deposit collections from one
  template have different `collection_id`s; a re-deposited key
  replaces; `native_id`s ascend.
- `tests/deposit/test_auth.py`: open `ask` routes and the Ask page
  never return a deposit collection; the deposit token opens nothing
  else.
- An operator's stop, like step 6: one LocalAGI agent on the node with
  `local_rag_url` pointed at Louise, memory stored, found, compacted.

In `migration-plan.md` this is a new step after 2, 4 and 5 (it builds
records, vectors and runs checks), independent of 9–12.
