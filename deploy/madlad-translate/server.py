"""MADLAD-400-3B-MT as the node's translation model, on the Intel GPU.

Why this exists (operator, 2026-09-18): the chat model translated one word
at a time — a query into 19 languages took 9.2 s idle and 17–28 s loaded,
one whole Ask entry 58–84 s — so "search every language" and "translate
the entries in full" were paid for on every search and every switch.
MADLAD is an encoder-decoder translation model (Apache-2.0, 419 languages:
Latin, Esperanto, Frisian, Welsh, Yiddish, Sanskrit, Tibetan among ours)
that translates a whole batch in one pass. A GGUF of it loads in Ollama
and in llama-server, but neither runs its encoder: Ollama hung and answered
500, llama-server wrote `<2cs><2cs>…` (node, 2026-09-18). llama.cpp's own
completion tool translates correctly, one text per process at 18 s of load.
So it runs here, on PyTorch XPU, the runtime verified on this card
(DEPLOYMENT "Running PyTorch on the Arc"), with transformers pinned to
4.46.3 (deploy/localai/backends/louise-madlad/requirements.txt).

The target language is a token in front of the text, `<2de>`; there is no
source language to name. The model saw sentences, and its window is 512
tokens, so a long text is cut into sentences packed up to `max_seg_tokens`,
every piece of every item goes through the GPU in length-sorted batches,
and the pieces are put back with the text's own paragraph breaks.

SERVED BY LocalAI since migration step 15f: deploy/localai/backends/louise-madlad
imports this module and answers `Predict` with `handle`, the batch as one JSON
prompt. Its HTTP server and its parking (rage.md §21) went with llama-swap at
15g (docs/notes/legacy-to-delete.md §8); its settings are the model YAML's.

`null` is an item whose target the model does not know — never the input
handed back as if it were a translation.

THE CARD IS SHARED — with the node's encoders, rerankers and chat model —
so memory is the budget, not speed. The decoder's cache grows with batch x
length. Measured on the node (2026-09-18), 96-token segments, a page of 12
entries: batch 32 peaked at 8.0 GB in 33.6 s, batch 64 at 11.5 GB in 20.4 s,
batch 256 at 26 GB in 15.4 s — about 14 GB of the card is free with every
other model loaded, so the default is 32; one entry is 5.9 s at either.
Every request's peak is logged, and the allocator's cache is handed back
after each one, so between requests the server holds its weights (~6 GB)
and what the runtime keeps (8.9 GB in all, measured after a batch-64 page).
Short segments are the cheap speed-up: 96 tokens instead of 160 took a
page of twelve entries from 50 s to 17 s (at batch 128).
"""
from __future__ import annotations

import os
import re
import threading
import time

#: an ISO code the platform stores -> the token MADLAD was trained with,
#: where the two name the same language differently
ALIASES = {"tl": "fil", "gla": "gd", "oji": "oj", "iw": "he", "in": "id", "ji": "yi"}

_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[.!?;:。！？；])\s+")
_SPACE = re.compile(r"\s+")


def target_token(code: str) -> str:
    code = code.strip().lower()
    return f"<2{ALIASES.get(code, code)}>"


def split_segments(text: str, max_tokens: int, count) -> list[list[str]]:
    """`text` as paragraphs of segments, each at most `max_tokens` by
    `count` where a sentence allows it. A paragraph's own line wraps are
    layout, not content (a Gutenberg page wraps at 70 columns), so it is
    flattened first; paragraph breaks survive."""
    paragraphs = []
    for para in _PARAGRAPH.split(text.replace("\r\n", "\n").replace("\r", "\n")):
        para = _SPACE.sub(" ", para).strip()
        if not para:
            continue
        segs: list[str] = []
        current = ""
        for sentence in _SENTENCE.split(para):
            pieces = [sentence]
            if count(sentence) > max_tokens:
                # a "sentence" longer than the window: cut on words
                words, pieces, piece = sentence.split(" "), [], ""
                for w in words:
                    trial = f"{piece} {w}".strip()
                    if piece and count(trial) > max_tokens:
                        pieces.append(piece)
                        piece = w
                    else:
                        piece = trial
                if piece:
                    pieces.append(piece)
            for p in pieces:
                trial = f"{current} {p}".strip()
                if current and count(trial) > max_tokens:
                    segs.append(current)
                    current = p
                else:
                    current = trial
        if current:
            segs.append(current)
        paragraphs.append(segs)
    return paragraphs


class Translator:
    """`count(text)` -> tokens; `generate(prompts, max_new)` -> one output
    per prompt; `known(token)` -> whether the model has that `<2xx>`."""

    def __init__(self, count, generate, known, batch: int = 32, max_seg_tokens: int = 96,
                 release=None):
        self.count, self.generate, self.known = count, generate, known
        #: hands the GPU allocator's cache back; returns the pass's peak (MB)
        self.release = release or (lambda: None)
        self.batch, self.max_seg = max(1, batch), max(16, max_seg_tokens)
        self.lock = threading.Lock()        # one GPU, one pass at a time

    def translate(self, items: list[tuple[str, str]]) -> list[str | None]:
        out: list[str | None] = [None] * len(items)
        # (item, paragraph, segment) -> prompt
        jobs: list[tuple[int, int, int, str]] = []
        shapes: dict[int, list[int]] = {}
        for i, (text, to) in enumerate(items):
            token = target_token(to)
            if not self.known(token):
                continue
            paragraphs = split_segments(text, self.max_seg, self.count)
            shapes[i] = [len(p) for p in paragraphs]
            for pi, segs in enumerate(paragraphs):
                for si, seg in enumerate(segs):
                    jobs.append((i, pi, si, f"{token} {seg}"))
        done: dict[tuple[int, int, int], str] = {}
        # similar lengths share a pass, so little of it is padding
        jobs.sort(key=lambda j: len(j[3]))
        with self.lock:
            try:
                for s in range(0, len(jobs), self.batch):
                    chunk = jobs[s:s + self.batch]
                    longest = max(self.count(p) for *_, p in chunk)
                    got = self.generate([p for *_, p in chunk], min(512, 2 * longest + 16))
                    if len(got) != len(chunk):
                        raise RuntimeError(
                            f"model gave {len(got)} outputs for {len(chunk)} inputs")
                    for (i, pi, si, _), text in zip(chunk, got):
                        done[(i, pi, si)] = text.strip()
            finally:
                self.peak_mb = self.release()
        for i, counts in shapes.items():
            out[i] = "\n\n".join(" ".join(done[(i, pi, si)] for si in range(n))
                                 for pi, n in enumerate(counts))
        return out


def handle(translator: Translator, body, max_items: int = 512) -> tuple[int, object]:
    """One request, `{"items": [{"text", "to"}, ...]}` -> (status, payload): 200, or
    422 and 413 for a request it refuses. The status is HTTP's, as the server
    answered it until 15g; the LocalAI backend says it in gRPC's words."""
    items = body.get("items") if isinstance(body, dict) else None
    if not isinstance(items, list) or not all(
            isinstance(it, dict) and isinstance(it.get("text"), str)
            and isinstance(it.get("to"), str) and it["to"].strip() for it in items):
        return 422, {"error": 'expected {"items": [{"text": str, "to": code}, ...]}'}
    if len(items) > max_items:
        return 413, {"error": f"{len(items)} items; at most {max_items} per request"}
    t0 = time.monotonic()
    out = translator.translate([(it["text"], it["to"]) for it in items])
    return 200, {"translations": out, "ms": round(1000 * (time.monotonic() - t0)),
                 "peak_mb": getattr(translator, "peak_mb", None)}


#: Invariant 12 for torch, the same switch as DAR-legal-it's and jina-v3's
#: (deploy/dar-legal-it/server.py): set in the MODEL's env, so the stated run
#: carries it. louise-python refuses to load this task without it (operator,
#: 2026-10-20: a backend that is not bit-exact on reprocessing is not used).
DETERMINISM_ENV = "LOUISE_TORCH_DETERMINISTIC"


def determinism(torch_mod, environ=None) -> bool:
    """torch's deterministic algorithms and oneDNN's deterministic primitives,
    when the model's env asks; an op with no deterministic kernel then fails
    the request rather than answering from one that may not reproduce."""
    environ = os.environ if environ is None else environ
    if environ.get(DETERMINISM_ENV, "").strip().lower() not in ("1", "true", "yes"):
        return False
    torch_mod.use_deterministic_algorithms(True)
    mkldnn = getattr(getattr(torch_mod, "backends", None), "mkldnn", None)
    if mkldnn is not None and hasattr(mkldnn, "deterministic"):
        mkldnn.deterministic = True
    return True


def load(model_id: str, device: str, dtype_name: str, beams: int | None = None):
    """The real model: (count, generate, known, languages, release). `beams`
    is the load call's (the LocalAI backend's option); unset, BEAMS from the
    environment, else one."""
    import torch
    from transformers import AutoTokenizer, T5ForConditionalGeneration
    determinism(torch)                      # before any kernel is chosen
    dtype = getattr(torch, dtype_name)
    tok = AutoTokenizer.from_pretrained(model_id)
    # STRAIGHT ONTO THE DEVICE. Loaded into host memory and then moved, the
    # weights left 4.6 GB the allocator never handed back: the container sat
    # at 7.1 GB of RSS with the model on the GPU (the operator saw 12.26 GB
    # during the load, 2026-09-18). device_map puts each tensor on the
    # device as it is read: 364 MB of RSS after the load, measured.
    model = T5ForConditionalGeneration.from_pretrained(
        model_id, torch_dtype=dtype, low_cpu_mem_usage=True, device_map=device).eval()
    unk = tok.unk_token_id
    vocab = tok.get_vocab()
    codes = {t[2:-1] for t in vocab if t.startswith("<2") and t.endswith(">")}
    # the platform's own codes too, where ALIASES maps them to a known one
    languages = sorted(codes | {a for a, c in ALIASES.items() if c in codes})
    beams = int(os.getenv("BEAMS", "1")) if beams is None else beams

    def count(text: str) -> int:
        return len(tok(text, add_special_tokens=False).input_ids)

    def known(token: str) -> bool:
        return tok.convert_tokens_to_ids(token) not in (None, unk)

    @torch.inference_mode()
    def generate(prompts: list[str], max_new: int) -> list[str]:
        enc = tok(prompts, return_tensors="pt", padding=True).to(device)
        ids = model.generate(**enc, max_new_tokens=max_new, num_beams=beams,
                             do_sample=False)
        return tok.batch_decode(ids, skip_special_tokens=True)

    def release():
        # the pass's peak, then the cache back to the card's other tenants
        peak = None
        kind = device.split(":")[0]
        if kind in ("xpu", "cuda"):         # cuda: an NVIDIA card or ROCm's
            card = getattr(torch, kind)
            peak = round(card.max_memory_reserved() / 2**20)
            card.empty_cache()
            card.reset_peak_memory_stats()
        return peak

    return count, generate, known, languages, release


def _release_host_memory() -> None:
    """Hand the host allocator's freed pages back to the system. What stays
    is the GPU runtime's: its first pass loads torch's compiled GPU kernels
    and keeps them in host memory (1.9 GB of Level Zero kernel binaries, read
    in its memory on the node, 2026-09-18), plus 0.8 GB of heap. What the
    pass freed stays with glibc otherwise."""
    import ctypes
    import gc
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass


def _drop_file_cache(model_id: str) -> int:
    """Evict the weights' files from the page cache; how many were advised.
    The load reads them through mmap, and those pages are charged to this
    container: TrueNAS showed 5.6 GB for it, 2.5 GB of which were the cached
    bf16 weights (operator, 2026-09-18). Nothing reads them again once the
    model is on the device. A hub id is resolved to its local snapshot."""
    root = model_id
    if not os.path.isdir(root):
        try:
            from huggingface_hub import snapshot_download
            root = snapshot_download(model_id, local_files_only=True)
        except Exception:                               # noqa: BLE001 — not cached here
            return 0
    advised = 0
    for base, _, names in os.walk(root):
        for name in names:
            try:
                fd = os.open(os.path.join(base, name), os.O_RDONLY)
            except OSError:
                continue
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                advised += 1
            except OSError:
                pass
            finally:
                os.close(fd)
    return advised
