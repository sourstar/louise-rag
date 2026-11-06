# The fingerprint: what it costs and what it changes

*2026-09-20. The fingerprint is specified on `main` (0d85f7f: 03 §3.3,
09 §9.12) and not built. This note records what it implies, measured on
the live node before building it. Three points need the operator (§8).*

## 1. The assumption holds on today's encoders

The fingerprint needs a run to reproduce its own vectors bit for bit.
The test took the 16 texts by 09 §9.12's rule (21 bytes to 97,932
bytes), sent them the way the worker does (one text a request,
`_embed_batch`'s shrink-on-refusal), and hashed them as little-endian
f32. There were three passes per encoder:
- plain;
- interleaved with an unrelated text, so the single slot's prompt cache
  could not answer;
- concurrent, four at a time.

| encoder | fingerprint | plain = interleaved = concurrent |
|---|---|---|
| gutenberg, Qwen3-4B Q4_K_M, ctx 8192 | 12.5 s / 17.1 s / 15.0 s | identical |
| pubmed, MedCPT-Article f16, ctx 512 | 1.1 s / 1.4 s / 0.6 s | identical |
| wikipedia, bge-m3 f16, ctx 1024 | 2.0 s / 1.0 s / 0.7 s | identical |

Concurrency is harmless here only because every server runs `-np 1`: it
takes one sequence per forward pass, and concurrent requests just queue.
**An engine that co-batches sequences (`-np > 1`, a PyTorch server
batching requests) is not guaranteed batch-invariant.** Its
fingerprint, taken alone, would then certify a path that production does
not take. Batch invariance is a property of each engine's configuration,
and the gate has to measure it (§4).

## 2. "After every batch" is unaffordable on gutenberg as written

The spec takes the fingerprint after every batch, before it commits. The
producers commit every `EMBED_COMMIT_S` (10 s) or 128 documents, and on
gutenberg a document is 5–145 s of encoder time. A 12.5 s fingerprint
per commit would cost between a third and all of gutenberg's
throughput, won back in the last deploy. The cost is the full-window
texts: four of the sixteen are near or past 8,192 tokens, and they are
there on purpose, because `-c`, `-b`, `-ub` and rope settings change
only long inputs.

Two ways out:
- **Cadence by cost (recommended).** Hold commits until a fingerprint,
  and size the commit group so the fingerprint costs a fixed fraction of
  the encoder's time. At 5% that is about 4 minutes of work per commit
  on gutenberg and about 20 s on pubmed and wikipedia. A crash or a
  mismatch then costs up to one group of re-embedding, and a SIGTERM
  waits one fingerprint before its commit. The guarantee stays whole: no
  vector commits without a matching fingerprint after it.
- **Cadence by suspicion.** Fingerprint at stream start and on a slow
  clock, and commit freely. Cheaper, but a change between two
  fingerprints commits unverified work, the window the operator asked
  to close.

## 3. A hole in resume: long documents write before any fingerprint

`upsert_book` writes a long document in slices of 64 chunks as it goes,
and a resumed document reuses every stored slice whose text matches.
The document is not marked done until it finishes, but its slices are
in the table. If the engine changes in the middle, the slices already
written came from the old run. The fingerprint after the document
catches the change and the document stays unmarked. **But the resume
then reuses those slices, because resume matches by text, not by run.**

Fix, with no schema change: on a mismatch, delete the unmarked slices of
the documents that were in flight. The window is at most three documents
per collection. Recording on each row the run that wrote it would also
work, but it is a Lance schema change and would need the operator.

## 4. The spec drops the batch-integrity check

`backend_gate` does two jobs:
- a probe baseline (cosine ≥ 0.995, re-checked every 30 minutes);
- a batch-integrity check: batched output must equal single output.

The second guards the defect that silently corrupted 714 books
(2026-07-20/21). 03 §3.3 as amended replaces the first and says nothing
of the second. The gate (`embedder check`) must keep it. It should
compare the fingerprint texts sent one at a time with the same texts
sent the way the peer contributes: its batch size and concurrency. A
difference means the engine is not batch-invariant at those settings,
and the peer must contribute at single-text, concurrency 1 on it.

## 5. Every legitimate bit change stops embedding until `--accept`

All of these change the fingerprint:
- a driver or compute-runtime update, which a TrueNAS update brings;
- a llama.cpp rebuild;
- a different `-t` on a CPU engine;
- choosing `@cpu` instead of `@xpu` at load (the model pool's device
  choice).

Under the spec, any of them stops that space's embedding until a person
runs `embedder check --accept`. That is the rule working: a different
run. But three consequences follow:
- **One accepted run per space is too few for the model pool.** A node
  that switches a model between devices must hold an accepted run for
  each variant. The fingerprint then says which one is live, and a
  switch between two accepted runs needs nobody.
- **Stopping must be loud.** It is a deferral, and a deferral looks like
  the idle GPU just fixed. The System page and the log must name the
  space and the command.
- **Whether a change can be accepted automatically** when the declared
  engine is unchanged and the gate passes (a driver update) is the
  operator's choice (§8). It is exactly the case a moving image tag also
  produces.

## 6. The query side should not lose its vector leg on a mismatch

The spec withholds a space's vector leg while its query fingerprint
differs. After a driver update that would put every search on lexical
only until someone accepts. But a query vector is never published: a
compliant engine answering queries harms nothing. So `--accept` should
gate what is PUBLISHED (bundles, attestations, benchmarks), not what is
used. On the query side, a mismatch should run the gate automatically
(about a minute) and continue if it passes. That would change the spec's
§3.3 query bullet; the operator decides (§8).

## 7. Smaller points

- **Coverage.** The sixteen are chosen by length alone. Any build or
  kernel change flips bits in all of them, so it is detected, and the
  full gate (512 texts, every script) then judges compliance. What could
  pass unseen is a change confined to one script's tokenisation that
  leaves all sixteen texts' bits unchanged. That is unlikely, and it is
  noted here only.
- **Proof across peers.** A peer that reproduces another's fingerprint
  exactly shows it runs the same engine on the same kind of device. That
  is stronger evidence than tolerance, and optional.
- **Migration.** The worker runs `backend_gate` (TOFU baseline, cosine
  0.995). The spec's statement lives in `app/hold/embedder.py`, which is
  not wired to the worker. A cheap first step is for `backend_gate` to
  record the fingerprint hash beside its baseline and compare hashes. It
  would then be exact at once, and the full statement would come later
  with the migration.

## 8. For the operator

1. **Cadence:** by cost (a fixed share of encoder time, about 4-minute
   commit groups on gutenberg), or by suspicion (stream start plus a
   slow clock).
2. **Accepting:** always by hand, or automatically when the declared
   engine is unchanged and the gate passes.
3. **The query side:** re-gate automatically and keep searching, or
   withhold the vector leg until accepted.

§3 (purge in-flight slices on a mismatch), §4 (the gate keeps batch
integrity) and §5's accepted run per variant are corrections, not
choices. They go into the spec with the answers.

## 9. Decided (operator, 2026-09-20; specs on main, dd9bef4)

The operator set the three questions aside for a simpler model:
**validate the model and its settings once, then be sure that what
serves each answer is the validated one.** The model container is a
trusted processing platform.
- It computes a **run identity** from what it actually launched: the
  image's running digest, the weights, the args and env, the device, and
  the driver and runtime.
- It states that identity on every answer (`Louise-Run`).
- A vector is written only from an answer naming a validated run,
  checked per answer, BEFORE the write.
- A new run is validated automatically.
- Written vectors carry no identity: the operator declined a schema
  change for it, and none is needed, because nothing unvalidated is
  ever written.

How that settles this note:
- §2, the cost: no fingerprint during production. It is taken once, at
  validation, as evidence.
- §3, the resume hole: closed. Slices are written only from validated
  answers; a document may mix two validated runs, both within tolerance.
- §4, batch integrity: the gate measures it (`batch_invariant`).
- §5, one accepted run per space: a statement per run, automatic.
- §6, queries: the same runs. A pending run serves queries; a failed
  run withholds the leg.
- New: `query_vectors` in the calibration member, so pubmed's MedCPT
  query encoder can be validated at all.

It depends on the model pool, the model container: only a front we run can state the run on
each answer (model-pool.md@c572c3b §4). Until then the worker keeps
`backend_gate`.
