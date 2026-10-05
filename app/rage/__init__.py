"""RAGE — the RAG Engine: the front of the node's model container.

docs/notes/rage-over-localai.md is the design (docs/notes/rage.md its
history); docs/03-checking.md §3.3 and docs/09-formats.md §9.12 are what it
answers to. One container serves every model the node runs — encoders,
rerankers, the chat and translation models — whatever its runtime
(llama.cpp, PyTorch, OpenVINO) and its device.

LocalAI LAUNCHES, LOADS AND EVICTS; RAGE STATES THE RUN OF EVERY ANSWER, read
from what LocalAI launched (observer.py) and never from a configuration —
and three things beside it: the card's clock while a person waits, the door
to LocalAI's admin routes behind the admin token, and the System page's panel
(front_localai.py). Since migration step 15f; until 15g a llama-swap front
decided placement itself (docs/notes/legacy-to-delete.md §8).

WHAT RUNS HERE IMPORTS NO GPU STACK (CLAUDE.md invariant 7) and nothing
that opens a manifest: RAGE's image carries `app/rage/`, `app/gpu_clock.py`,
`app/gpu_memory.py` — the card's memory as its driver counts it, the one
place a kernel driver is named — and `app/hold/codex.py`, for the ONE
definition of canonical JSON and of the hash; and no more of the platform
(tests/rage/test_localai_deploy.py holds the image to that). Not the GPU
probe either: it sees the host's processes, and nothing imports it
(tests/test_gpu_panel.py). The models are LocalAI's child processes; torch
lives in them.

    model.py      a device, a model id, a configuration error
    localai_config.py  rage.toml: the cards, nothing of the lifecycle
    localai.py    the HTTP client of the local LocalAI
    loadopts.py   a LocalAI load call as 09 §9.3's engine `options`
    procfs.py     what a process really is: argv, environment, start, card memory
    runs.py       the image, the weights digest, the driver, the run identity
    observer.py   what LocalAI runs, and the run each answer states
    store.py      the runs this front has stated, kept across its restarts
    clock.py      the card's clock, by the level of the requests in flight
    wire.py       what the front says on the wire: the run header, the refusal, the token
    front_localai.py   the HTTP surface: /upstream, /louise/models, /louise/runs, /louise/gpu
    __main__.py   boot: refuse a tag, start LocalAI, serve
"""
