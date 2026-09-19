# Dedicating the GPU to one collection at a time — a deferred design

*Operator, 2026-09-10. **Not to be built yet**: it is to be taken up when
the llama server hosting multiple embedders is designed, because the two
decisions are the same decision seen from either end.*

*2026-09-19: the premise below that "the Arc fits one model server at a
time" is stale. The GPU panel measured eight servers resident at once,
25.7 of 34.2 GB (STATUS.md, 2026-09-18). A slot is now the fallback for
whatever does not fit together — [`rage.md`](rage.md) §5.*

## The idea, in the operator's terms

Use the GPU only for the **heavy lifting**, and dedicate it to one
collection at a time rather than letting every collection reach for it.
Two ways of slicing that, depending on what the work is:

* **Per batch** — when processing one or more batches arriving *from the
  network*. The unit is the batch: the GPU is given to the collection
  whose batch is in hand, for that batch.
* **Per time slot** — when embedding more than one collection locally.
  The unit is a slot: the GPU belongs to one collection for the slot,
  then moves on.

**In both cases the slot is roughly ONE HOUR.**

## Why an hour, and why the number matters more than it looks

A model swap is not free, and the hour is what makes it amortise. The
reference node's Arc fits one model server at a time (see below), so
moving the GPU between collections means unloading one model and loading
another — seconds to a minute of GPU idle, plus the server's own start-up
before it answers. At a one-minute swap an hourly slot spends under 2 %
of the GPU on switching; at a five-minute slot it spends 20 %.

It also has to be long enough to be worth the accounting. 04 §4.5's
rendezvous already hands a peer whole *shards*, and 07 §7.7 makes
`vectors build`'s unit the batch in flight to the encoder — so an hour is
comfortably several shards' worth of committed work at the rates
measured in [`scheduling.md`](scheduling.md) §4, and a slot boundary
never lands mid-commit.

And it is short enough to stay fair. A collection waiting behind three
others waits three hours, not three days, which is the difference between
a queue and a starvation.

## What forces it — the constraint it exists to answer

The Arc Pro B70 **fits one model server at a time.** That is not a
scheduling preference, it is the hardware, and today it is handled by
hand: on 2026-09-10 the GPU was taken from a running `dnabert2-xpu`
container so the Qwen3-4B encoder could serve gutenberg, and the genome
collections then logged *"embed endpoint unreachable this boot —
proceeding without a hard refuse"* until their CPU alternative was
started. That is the manual version of exactly this design, performed
badly, by a human, once.

`scheduling.md` §2 already records the shape: *"the encoders — GPU: all
collections that do not own theirs; CPU: one per collection. GPU work is
serial by doctrine."* **Serial by doctrine is the thing this proposal
makes serial by construction**, and gives a unit and a period.

## Why it waits for the multi-embedder llama server

The two are one design. A slot scheduler needs to be able to *cause* a
swap — to say "this collection now" and have the right model answering —
and today nothing can: each encoder is its own TrueNAS app on its own
port, started and stopped by hand. Build the scheduler first and it has
no lever; build the server first and the scheduler is a small thing on
top of it.

So the open question is not "how long is a slot" — that is answered, an
hour — but **what the server exposes**: whether a slot is requested by
loading a named model, by routing to an already-warm one, or by the
scheduler starting and stopping servers itself. Whatever that turns out
to be is what the slot mechanism is written against.

## What to check when it IS built

`scheduling.md` §6's checklist applies whole; these are the ones this
design will stress:

* **A per-round cost must scale with the work available, never with the
  number of collections.** A slot table walked per round is fine at eight
  collections and is the wrong shape at eighty.
* **"Did work" is not "made progress".** A collection that holds a slot
  and embeds nothing — because its archive already covers what it would
  have embedded — must yield the rest of the slot, not sit on it.
* **The unit must survive the boundary.** 07 §7.7: an interrupted stage
  finishes its current unit and commits. A slot expiring is not a signal
  to abandon a batch in flight to the encoder; it is a signal to stop
  after it.
* **Measure first.** `louise dev schedule-report` before proposing
  anything, and a scheduling change whose commit message has no numbers
  in it is a guess.
