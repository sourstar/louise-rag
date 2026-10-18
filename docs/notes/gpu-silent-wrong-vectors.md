# The B70 returns wrong vectors for some inputs, silently (found 2026-10-18)

*Found while reproducing Wikimedia's Wikidata vectors
([wikidata-plugins.md](wikidata-plugins.md)). The cause is found (§2): torch's fp32 softmax. Open: whether
any live collection holds such vectors (§5 is the check, and its results).*

## 1. What was seen

jina-embeddings-v3 (torch 2.14.0+xpu, `louise-python`'s own Python, weights
`ab036b0` with remote code `bd55a5e`) was run on 1,003 entity texts, on the
B70 and on the CPU. The model, weights, code and texts were identical.

| text (Wikidata entity, chunk) | tokens | CPU: cosine to Wikimedia's vector | B70: same |
|---|---|---|---|
| Q9142200 *Agnes 1946* (1964 film), 1 | 588 | 0.999958 | **−0.067735** |
| Q9174055 Bjørn Brinck-Claussen, 2 | 1,026 | 0.999930 | **0.016667** |
| Q9174055 Bjørn Brinck-Claussen, 3 | 509 | 0.999958 | **−0.037523** |
| Q9208205 "24-hour urine collection", 1 | 8 | 0.963090 | 0.963090 |

- **3 of 1,003 chunks (0.3 %) came out wrong on the B70.** Each is a finite,
  unit-norm vector unrelated to its text: its nearest neighbour among the
  1.3 M rows of the file it was compared with is at cosine ≤ 0.2, where a
  real entity text is at ~0.6.
- **The same wrong vector comes back every run** (−0.0677 in two separate
  runs). So a check that re-embeds on the same card agrees with it, and so
  does the calibration gate. Only another device tells them apart.
- **It is not the NaN corruption already known** (`embeddings._numeric_failure`
  retries a non-finite vector). These vectors are finite and normalised, and
  nothing downstream refuses them.
- Lengths 509, 588 and 1,026 tokens fail; 8 tokens and most others do not.
  No length rule is established yet.

## 2. The cause: torch's fp32 softmax on the B70, for some row lengths

Isolated the way `llama-intel-arc-b70`'s KNOWN-ISSUES.md isolated
llama.cpp's oneDNN drift (that repo's full notes are only in the NAS copy,
`/root/llama-intel-arc-b70`). Each step held everything else fixed:

1. **Every module, CPU against B70, for Q9142200 (597 tokens):** the first
   output that diverges is layer 0's attention probabilities (cosine 0.32).
   Everything before it agrees.
2. **The inputs to attention agree:** the QKV projection and the rotary
   encoding give cosine 1.0 on both devices.
3. **The product agrees:** `einsum("bthd,bshd->bhts")`, `matmul` (strided or
   contiguous) and per-head products all match from identical inputs.
4. **The softmax does not:** on the same scores, the B70's `torch.softmax`
   returns values up to 5.86 (a softmax is in [0, 1]), cosine −0.12 to the
   CPU's. At 94 tokens the two agree to 2e-7.
5. **The sweep:** `torch.softmax(x, dim=-1)` on random fp32 input, every row
   length from 1 to 2,048, CPU against B70 (torch 2.14.0+xpu, the card nearly
   full at 31.4 of 32 GB):

| row length | result |
|---|---|
| 1–512 | correct |
| **513–768** | **wrong** (errors up to 18) |
| 769–1024 | correct |
| **1025–1280** | **wrong** |
| 1281–1536 | correct |
| **1537–1792** | **wrong** |
| 1793–2048 | correct |

  fp16 and bf16 are correct at 597 (errors 0.002 and 0.016, their own
  precision). A 3-D tensor fails too. So: **every second block of 256 above
  512, in fp32.** It is a kernel that picks its tiling by row length, the
  same family as oneDNN's GEMM heuristic in the B70 repo, and like
  llama.cpp's batching corruption (STATUS, `EMBED_BATCH=1`) it repeats
  exactly, run after run.

**What is exposed:** any torch model on the B70 that runs a softmax in fp32
over 513–768, 1,025–1,280, … positions, i.e. attention over a text of that
length.
- **jina-v3:** its own eager attention, `torch.softmax(..., dtype=v.dtype)` in
  fp32.
- **DAR-legal-it** (normattiva, live): `sdpa`, so whether it reaches this
  kernel is the measurement in §5.
- **MADLAD:** T5 computes its softmax in fp32, but its segments are ≤ 96
  tokens, below 513.

llama.cpp (gutenberg, wikipedia) and OpenVINO (pubmed) have their own
kernels and are checked separately in §5.

**Not reported upstream** (searched 2026-10-18: intel/torch-xpu-ops and
pytorch/pytorch issues). The nearest entries are a closed warp-softmax test
failure on very large tensors, and an open migration of the softmax kernels to
SYCL free functions. **Still present in torch 2.14.1+xpu** (the stock
`intel-transformers` backend's): the same three ranges, the same errors. The
reproducer, for the operator to file at intel/torch-xpu-ops:

```python
import torch
x = torch.randn(1, 1, 597, 597) * 4                      # fp32, last dim 597
err = (torch.softmax(x, -1) - torch.softmax(x.to("xpu"), -1).cpu()).abs().max()
print(torch.__version__, float(err))                     # B70: 18.03 (a softmax is in [0, 1])
# every L in 513–768, 1025–1280, 1537–1792 fails; 1–512, 769–1024, … do not;
# fp16 and bf16 are right
```

**The fix in Louise** (deploy/jina-embeddings-v3/server.py): every
`SelfAttention` of jina-v3 through `scaled_dot_product_attention`. Measured:
- the replacement equals the eager attention within 6e-7 on both devices,
  at 37, 597 and 1,100 positions, padding included;
- on 1,003 Wikidata chunks on the B70, every vector is within 1e-4 of the
  CPU's: 864 exact to the archive, the same 864 as on the CPU;
- 30 % faster (61 s against 87 s).

## 3. What it is not

- Not the text: on the CPU the same text matches Wikimedia's vector at
  0.99996.
- Not the batch: one text per call, in both runs.

- Not the card's memory: the failures depend on length alone and repeat
  exactly, with the card at 31.4 of 32 GB.

## 4. What follows

- jina-v3 on the B70 is not usable for vectors until this is fixed
  (promise 8). Its task is built, not deployed.
- Every live collection computed on the card must be checked against the
  same model on another device (§5) before anything else proceeds.

## 5. The check of the live collections

Per collection: a random sample of stored vectors with their chunk texts,
re-embedded with the same model and settings on the CPU, compared row by row.
A row is flagged below the collection's tolerance.

| collection | encoder, engine on the card | CPU reference | result |
|---|---|---|---|
| normattiva | DAR-legal-it, torch XPU (`louise-python`) | the same code, `device:cpu` | **clean**: 597 chunks, 49 of them in the failing length ranges; DAR runs SDPA, not the eager softmax |
| pubmed | MedCPT article, OpenVINO GPU (`louise-python`) | OpenVINO CPU (`vega-medcpt-article-ov-cpu`) | **the card is clean; 74 of 1,088 stored vectors are not this function's** — below, and [char-cut-truncation.md](char-cut-truncation.md) |
| gutenberg | Qwen3-Embedding-4B GGUF, llama.cpp SYCL | llama.cpp CPU | not measured: the CPU twin did not finish 1,000 chunks of a 4B model within an hour; to run at night (quiet hours) |
| wikipedia | bge-m3 GGUF, llama.cpp SYCL (layer 2; layer 1 is wikivec's archive) | llama.cpp CPU | not measurable yet: `vectors-sample` draws over a snapshot's parts and wikipedia has none; the switch to parser 5 measures it ([wikipedia-qids.md](wikipedia-qids.md) §4) |

**Pubmed, 2026-10-18** (seed `aa7b98d0…`, `tools/cross_device_check.py --explain`). On
all 74 failing chunks the card now and the CPU twin agree (cosine 0.99995 or more), while
the stored vector agrees with neither (0.95–0.9999): the stored vectors were made from
another input. All 74 are NCBI's imported MedCPT vectors (`genesis:medcpt-pubmed`), and
they are the LONG abstracts (median 2,255 characters, against 1,183 for the imported
vectors that pass). Re-run on the card with the platform's character cut disabled, so
the encoder truncates at its own 512 tokens as NCBI did, 73 of the 74 reproduce and 18
locally embedded vectors fail instead. The cause is the platform's cut, not the card:
[char-cut-truncation.md](char-cut-truncation.md).
