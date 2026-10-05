# Deposit collections — Louise as LocalAGI's knowledge base

**Status: ADOPTED 2026-09-29 — written into 01, 02 §2.9, 03 §3.2, 04
§4.4, 06 §6.7, 07 and 09 §9.13, §9.17; migration step 16. Nothing
built.** Where this note and those documents differ, the documents win.
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

### A0 — Compliance, not proof (operator, 2026-09-29)

*"A non-shareable collection has only to fulfil the criteria of
compliance: I don't need to demonstrate to others that the vectors have
not been maliciously modified, but I still need to ensure that I keep
producing consistent embeddings."*

So a `never-share` collection carries the machinery that keeps **its
own vectors consistent**, and none of the machinery that **proves them
to someone else**:

| kept — consistency | dropped — proof to others |
|---|---|
| the Codex, pinning the space; a mismatch refuses that collection at start (invariant 1) | snapshots, record shards, the drag (02 §2.2–2.6) |
| the calibration gate on the run, and the per-answer `Louise-Run` check before every vector write (03 §3.3) | signatures, attestations, bundles (03 §3.5–3.6) |
| deterministic cleaning and chunking (invariant 3), so a re-embed cuts the same chunks | `records-vs-origin` and the published validation plan |
| a local `vectors-sample` on a timer: re-embed a few chunks through today's validated run and compare with the stored vectors at `tolerance` — the only thing that catches an engine that drifted after passing the gate | |
| the deposited bytes (A2), because re-embedding under a new encoder and `GetEntryContent` both need them | |

The Codex still names `validation` and `update` because the schema
(09 §9.3) requires every field; for a never-share collection they are
written and not run.

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
| `modality` | text | text, Markdown and PDF, all extracted to text (D5) |
| `mutability` | immutable | a deposit is replaced whole, never edited in place |
| `native_id_order` | numeric | A5 |
| `corpus_provider` / `corpus_attribution` | "the depositor" / "Deposited by a client of this node." | declared, like every plugin's, green included |

- `discover` lists the store; `fetch` reads the deposited bytes;
  `parse` takes `.txt` and `.md` as UTF-8, cut into sections on
  Markdown headings (one untitled section otherwise), and `.pdf`
  through pinned `pypdfium2`, one section per page with the PDF's own
  metadata kept (D5). Anything else — and text that is not valid UTF-8
  — is refused at upload (415), never stored and never guessed at.
- `origin_hash` is BLAKE2b-128 of the bytes exactly as deposited; the
  record's `origin` is the store path relative to its base. The origin
  is kept for as long as the record exists, and both go together (A4),
  so a re-embed or a re-parse always has its input.
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
- **`space`** — the default encoder is **LocalAGI's own default,
  `granite-embedding-107m-multilingual`** (D6): IBM, 107M parameters,
  384 dimensions, 512-token context, Apache-2.0, Italian among its
  languages; RAGE serves it through llama.cpp from a GGUF build
  (`bartowski/granite-embedding-107m-multilingual-GGUF`) — or, as
  LocalAGI itself gets it, from LocalAI's model gallery at creation
  ([`weights-fetch.md`](weights-fetch.md) §3), the gallery's `sha256`
  being the Codex's weights digest. It is named in `platform.toml`
  `[deposit] encoder`. The FIRST deposit collection
  created on it runs `create`'s calibration once (about a minute);
  every later one copies that space block whole — encoder, engine,
  calibration, envelope, tolerance — and skips it, **the bytes being
  identical** (CLAUDE.md RULE 1: the default, reported, not asked).
  **The operator may choose another encoder**: on the command line
  with `--space-from CODEX` or `--encoder URL`; for collections LocalAGI
  creates (its create carries no model), per name in `platform.toml`
  `[deposit.collections.<name>] space_from`, or node-wide by changing
  `[deposit] encoder`. The choice is fixed when the collection is
  created (it is in the Codex); changing it later is a sister with a
  new space (`hold collection derive --space-from`), whose deposits
  are re-embedded from `origin/` — minutes, not days. Whatever the
  encoder, it must be `modality: text`, and `create` refuses anything
  else.
- **`layers`** are the deposit plugin's default: one layer, `fixed`.
  Its window is sized to the encoder's context (512 tokens for the
  default). `space_id` hashes layers, so deposit collections on one
  encoder share one space — an agent's memory and its URL sources (A9)
  are one ranked list — while a curated collection, with its own
  chunker, is another space, fused by RRF (05 §5.1).
- **`update`, `validation`, `work`** — written because the schema
  (09 §9.3) requires them, and not run (A0).
- `credits` holds the single `corpus` entry; `publication: "open"`,
  `certifier: null`.

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
5. there are no shards or snapshots to purge: A0 drops them, so 02
   §2.3's "a superseded version is kept" and 02 §2.2's
   `delta.tombstoned` never arise. Promise 7 is about what is
   *published*; a never-share collection publishes nothing.
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
the existing verb with the `deposit` plugin and its existing `--name`;
the recipe is the plugin's defaults, so none is needed. It
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

### A7b — How data arrives: one file per request

LocalAGI never sends a folder. Its paths into a collection are:

- **agent memory** — `Store(text)`, one upload per memory: each user
  message, the whole conversation, or its summary, per the agent's
  memory mode (`core/agent/knowledgebase.go`);
- **the agent's own action** `add_to_knowledge_base` — one text;
- **compaction** — one summary per period, then the originals deleted;
- **the Web UI** — one file per upload (a single `<input type=file>`,
  no `multiple`, no directory picker);
- **sources** (D3) — one URL, re-fetched on a timer. **Added by a
  person in the Web UI, never by an agent**: no agent action or memory
  path calls `AddSource` (only `webui/collections_handlers.go` does).

So the surface is one deposit per request, and nothing more is needed
for LocalAGI. Bulk loading of a folder is a node operator's job and
already has its form: a folder is a corpus, and a corpus becomes a
collection through a plugin (06 §6.6) — curated, and then visible to
agents read-only (D2). If a deposit-side bulk path is wanted later, it
is `hold records fetch` over a mounted folder with the `deposit`
parser, not a new route.

### A9 — URL sources, exactly as LocalAGI and LocalRecall do them (D3)

The behaviour is LocalRecall's (`rag/source_manager.go`, `rag/sources/`
at `4cae67b`; LocalAGI embeds it as `localrecall v0.6.5`), reproduced.
A source is a **subscription inside the collection**:

- **Attached to the collection** that holds the agent's memory and
  uploads — no separate collection. The subscription (URL, interval in
  minutes, default 60) is desired state, so it lives in
  `collections/<name>.toml` `[[sources]]` (invariant 10).
- **One URL = one entry**, key `source-<collection>-<sanitised url>.txt`
  (LocalRecall's `sanitizeURL`, byte for byte: lower-cased, `://` `/`
  `?` `&` `=` `#` `@` `:` `.` `+` and space to `-`, runs of `-`
  collapsed, trimmed, cut at 255), metadata `{url}`:
  - a URL ending `.git` is shallow-cloned (depth 1, HEAD) and its text
    files concatenated;
  - a URL ending `sitemap.xml` has every listed page fetched and the
    texts joined;
  - anything else is one page, HTML converted to text.
- **Refreshed by replacement**: when the interval has passed the URL is
  fetched again and the entry replaced whole (A5). A failed or empty
  fetch keeps the previous entry.
- **An ordinary entry**: listed by `ListEntries`, searched with memory
  and uploads, readable, deletable. Compaction skips it (no date
  prefix).
- **`RemoveSource`** drops the subscription and erases the entry (A4).
- **Fetched at once when added.** LocalRecall persists a new source but
  does not put it on the running ticker, so as read it is first fetched
  at the next restart; Louise fetches it immediately, which is what
  `RegisterCollection`'s immediate update shows was intended.

**The licence gate (operator, 2026-09-29).**

- **Git with the node's key: implicit.** A repository cloned over SSH
  with the operator's own key is one the operator is authorised to
  read. The key is referenced by path, `GIT_PRIVATE_KEY_FILE`
  (invariant 8) — LocalRecall's `GIT_PRIVATE_KEY` carries it base64 in
  an env value, which Louise never does.
- **Every other URL: a generic disclaimer, accepted once per node.**
  Its text ships with the platform, byte-exact
  (`app/plugins/deposit/WEB-DISCLAIMER.txt`): *all fetched content is
  the property of its owners; its licences must be respected; the
  collection is never shared, and the operator who adds a source is
  liable for any infringement.* Accepting it records the timestamp and
  the sha256 of that text, as every acknowledgement is recorded
  (invariant 9's form). Until it is accepted, `AddSource` answers 409
  with *"accept the web-sources disclaimer in Louise first"*; nothing is
  fetched.
- This is compatible with invariant 9 as written: a deposit collection
  is green (D4) and never shared, and the disclaimer is the operator's
  standing acceptance for the URLs they subscribe it to.

The one Louise rule kept that LocalRecall does not have: every fetch
draws from the transfer manager's budget (invariant 5). It changes no
behaviour LocalAGI can see.

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
- **No DOCX or other binary formats but PDF in v1**, and no OCR (D5).
- **No compatibility beyond what LocalAGI's client sends.** LocalRecall's
  other routes (raw file download, its own UI) are not a target.
- **No change to any curated collection.** They gain a read-only view,
  nothing else.

## 5. Decisions for the operator

**All six decided by the operator, 2026-09-29:** D1, D2 and D4 as
recommended; D5 with PDF in v1 through `pypdfium2`; D3 with URL sources
in v1 exactly as LocalAGI does them, git authorised by the node's key and
other URLs behind a one-time disclaimer (A9); D6 with LocalAGI's
own default encoder, overridable (A3).

- **D1 — Adopt A0–A9 as the amendment to 01, 02, 04, 06 and 07?**
  Adopted. 09 needed two additions after all: the deposit plugin's
  `origin.spec` (§9.17) and the web-sources acceptance file (§9.13).
- **D2 — Do curated collections appear to LocalAGI?** *Accepted, as recommended:
  yes, read-only.* It is what makes the integration worth more than
  LocalRecall: an agent searches Normattiva beside its own memory. The
  cost: LocalAGI's UI will offer "upload" and "reset" buttons on them,
  which answer 409.
- **D3 — `sources` routes. DECIDED: as LocalAGI and LocalRecall do
  it (A9), with git authorised by the node's key and every other URL
  behind a one-time disclaimer.**

  *How LocalRecall does it* (read at `4cae67b`): a goroutine ticks
  every minute and re-fetches each URL whose interval has passed —
  `.git` is shallow-cloned and every text file concatenated, a
  `sitemap.xml` has every page fetched and joined, anything else is one
  page through `html2text`. The result is ONE entry,
  `source-<collection>-<url>.txt`, replaced whole each time. No
  bandwidth budget, no terms, no robots.txt, and no record of which
  page each passage came from. A9 reproduces this behaviour; it adds only the
  disclaimer, the key-by-path and the transfer budget.

- **D5 — Which formats in v1. DECIDED: text, Markdown and PDF.**

  *How LocalRecall does it:* `.txt`, `.md` and `.pdf` are chunked;
  any other file is stored "raw-only" — listed, downloadable, never
  searched. PDF text comes from PDFium compiled to WebAssembly
  (`klippa-app/go-pdfium`, no cgo), under a 60 s timeout. For Louise
  the equivalent is `pypdfium2` — the same PDFium, Apache-2.0/BSD,
  prebuilt wheels — pinned, because extraction must be deterministic
  (invariant 3) for a re-embed to cut the same chunks. That is a new
  dependency and needs the operator's yes — **given 2026-09-29**.
  Promise 9 applies to it: the PDF's own metadata (title, author,
  subject, keywords, creation and modification dates) is kept — `title`
  and `creators` where they have a natural home, `extra` otherwise —
  and the page structure is kept as sections, one per page. Extraction
  runs under a timeout, and a PDF that yields no text (a scan) is
  refused at upload with 415 rather than stored unsearchable: OCR is
  not in v1.

- **D6 — the encoder. DECIDED: LocalAGI's default,
  `granite-embedding-107m-multilingual`, overridable by the operator
  (A3).**

  *LocalAGI's default* is `granite-embedding-107m-multilingual`
  (`cmd/env.go`; LocalRecall's docker-compose uses the same, and its
  own binary has no default at all): IBM Granite, 107M parameters,
  384 dimensions, 512-token context, Apache-2.0, Italian among its
  languages, served by LocalAI. In HTTP mode LocalAGI sends no model —
  the server decides — so Louise's choice is free. Granite costs one new
  model in RAGE and one calibration, once; it is small enough to stay
  resident beside the others. `qwen3-embedding-4b`, already in RAGE and
  calibrated, remains the obvious override where retrieval quality
  matters more than footprint.

  *How LocalRecall does it:* one `EMBEDDING_MODEL` name for every
  collection. On chromem nothing records which model wrote a vector,
  so changing the setting silently mixes spaces in every existing
  collection. On postgres the model NAME and dimension are stored per
  collection, and a changed name re-embeds the whole collection at
  start. Either way the identity is a string: new weights, a new
  quantisation or a new server build behind the same name are never
  noticed — exactly the drift A0's gate and `vectors-sample` exist to
  catch.

## 6. How it would be proved done

- `tests/deposit/test_localagi_contract.py`: every request of §1,
  byte-for-byte as `pkg/localrag/client.go` at `7e0947d` sends it,
  replayed against the app; every response decoded the way that client
  decodes it (status code, envelope, the unwrapped upload body).
- `tests/deposit/test_erasure.py`: A4's no-trace assertion, including
  Lance's old versions and unmerged index segments.
- `tests/deposit/test_deposit_identity.py`: two deposit collections from one
  template have different `collection_id`s; a re-deposited key
  replaces; `native_id`s ascend.
- `tests/deposit/test_auth.py`: open `ask` routes and the Ask page
  never return a deposit collection; the deposit token opens nothing
  else.
- `tests/deposit/test_sources.py`: entry keys equal LocalRecall's
  `sanitizeURL` on its own test cases; `AddSource` before the
  disclaimer answers 409 and the transfer manager records zero
  requests; after it, the entry exists once and is replaced whole on
  refresh; a failed refresh keeps it; `RemoveSource` leaves no trace
  (A4).
- An operator's stop, like step 6: one LocalAGI agent on the node with
  `local_rag_url` pointed at Louise, memory stored, found, compacted.

In `migration-plan.md` this is a new step after 3 and 4 (the gate and
`vectors build`); A0 removes its dependence on 2 and 5. Independent of
9–12.
