"""RAGE — the RAG Engine: the front of the node's model container.

docs/notes/rage.md is the design; docs/03-checking.md §3.3 and
docs/09-formats.md §9.12 are what it answers to. One container serves every
model the node runs — encoders, rerankers, the chat and translation models —
whatever its runtime (llama.cpp, PyTorch, OpenVINO) and its device.

THE FRONT OWNS EVERY DECISION. It admits a load against the device's
memory, loads and unloads, computes the run identity from what is actually
running and states it on every answer (`Louise-Run`), holds the card's clock
while a person waits, and guards the operator's routes with the admin token.

llama-swap RUNS THE PROCESSES AND DECIDES NOTHING. Its yaml is generated
from `rage.toml` (swapyaml.py): one group holding every entry, no swapping,
no exclusivity, no TTL, bound to localhost. With no group at all llama-swap
runs one model at a time — its documented default — so the group is what
makes it policy-free, not its absence.

WHAT RUNS HERE IMPORTS NO GPU STACK (CLAUDE.md invariant 7) and nothing
that opens a manifest: RAGE's image carries `app/rage/`, `app/gpu_clock.py`,
`app/gpu_memory.py` — the card's memory as its driver counts it, the one
place a kernel driver is named — and `app/hold/codex.py`, for the ONE
definition of canonical JSON and of the hash; and no more of the platform
(tests/rage/test_placement.py holds it to that). Not the GPU probe either:
it sees the host's processes, and nothing imports it
(tests/test_gpu_panel.py), so a card's name and size are declared in
`rage.toml`. What RAGE reads of the card is how much of it is USED, to count
a stranger it cannot otherwise see (placement.py: `card_memory`). The models
are child processes; torch lives in them.

    model.py      rage.toml and rage.d/<id>.toml → RageConfig
    swapyaml.py   RageConfig → llama-swap's yaml
    procfs.py     what a child process really is: argv, environment, memory
    runs.py       the weights digest, the engine as launched, the run identity
    admission.py  does this load fit
    clock.py      the card's clock, by the level of the requests in flight
    store.py      the runs this front has stated, kept across its restarts
    swap.py       the HTTP client of the local llama-swap
    front.py      the HTTP surface: /upstream, /louise/models, /louise/runs
    __main__.py   boot: refuse a tag, write the yaml, start llama-swap, serve
"""
