"""RAGE — the RAG Engine (docs/notes/rage.md; operator, 2026-11-01).

Louise's own engine: the one code path every collection's embedding, and
every question's encoder call, goes through. It chooses what to embed, fills
the requests, shares the card between collections with a person's query
first, admits only answers from validated runs, and measures itself. It runs
no model (invariant 7), and it is not an *engine* in 09 §9.3's sense — that
is always the encoder's; RAGE's settings are `batch` and `concurrency`.

Its parts, as `app.rage` names them:

- `dispatch` — the gate per encoder URL, the levels, the interactive claim;
- `settings` — the settings in effect for a collection on this node: its
  file's, then the recommendation for this card (app/hold/rage_settings.py);
- `card_record` — the per-second record of the card, the gates and the work done;
- `bench` — a collection's row for a window of that record.

Named here and living beside the code they call until it next changes
(wrap, do not rewrite): the feed (`worker._embed_producer`,
`worker._embed_parallel`), the fill (`embeddings.Coalescer`,
`embeddings.coalescing`, `embeddings.batch_size`), the admission
(`run_check`, `hold.runs`).

Nothing is imported here: each part is imported by name
(`from app.rage import dispatch`), so importing one never pulls the others
and no import cycle runs through the package.

Until 2026-11-01 "RAGE" named the model pool, VeGa's predecessor
(model-pool.md@c572c3b); `app/rage/` paths in commits before 2026-10-18
are that pool's, deleted with it.
"""
