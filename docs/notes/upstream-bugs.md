# Bugs to notify upstream — the vendors' stacks

*The operator files these. LocalAI's own list is
[`localai-bugs.md`](localai-bugs.md); this one is for the stacks under
it — PyTorch on Intel XPU today. Started 2026-10-20 (operator: "add this
bug to the list of bugs to be notified").*

| id | where | what | for us | upstream |
|---|---|---|---|---|
| V1 | torch 2.14.0+xpu on the Arc Pro B70 (`intel/torch-xpu-ops`, `pytorch/pytorch`) | **Non-reproducible vectors for SHORT inputs with torch's defaults.** jina-embeddings-v3 (an XLM-RoBERTa, attention through `scaled_dot_product_attention`): 512 calibration texts embedded over 2 fresh loads × 2 shuffled orders, 204 bit-identical; the 308 that differ are the short texts (0–241 average 48 tokens, 448–511 average 35) while the long block (242–447, average 5,967 tokens) is identical but for two. `torch.use_deterministic_algorithms(True)` (+ `torch.backends.mkldnn.deterministic`) makes it 512/512 on the same card and build. So the default kernel choice for small shapes reduces in an order that is not fixed run to run; the deterministic path is. Not isolated to an op (a module-by-module comparison as for V2 is the next step if the vendor asks). DAR-legal-it (a GTE model, sentence-transformers, the same stack) shows the same symptom — 1/4 bit-identical against itself, a calibration fingerprint that moves across byte-identical trees — and is measured with the switch next (2026-10-20). | invariant 12: every torch run on a card now carries `LOUISE_TORCH_DETERMINISTIC=1` in its model env (deploy/jina-embeddings-v3/server.py, deploy/dar-legal-it/server.py); approved-encoders.md "torch" | **not filed** (searched 2026-10-20: nothing on short-shape nondeterminism for XPU) |
| V2 | torch 2.14.0+xpu and 2.14.1+xpu on the B70 (`intel/torch-xpu-ops`) | **fp32 `torch.softmax` is WRONG for row lengths 513–768, 1025–1280, 1537–1792**: finite, normalised, unrelated results, the same every run; fp16 and bf16 right ([gpu-silent-wrong-vectors.md](gpu-silent-wrong-vectors.md) §2, with the eight-line reproducer: `(softmax(x) − softmax(x.to("xpu")).cpu()).abs().max()` = 18.03 at 597). | the jina-v3 task routes attention through SDPA (`use_sdpa`), which computes the same function through a fused kernel that is right; every torch model's vectors on the card are checked against the CPU's (gpu-silent-wrong-vectors.md §5) | **not filed** (searched 2026-10-18: the nearest is a closed warp-softmax test on very large tensors and an open migration of the softmax kernels to SYCL free functions) |

What a report needs, both: the card and driver line (`hold embedder run` prints
it: kernel 6.18.52, xe, libze-intel-gpu1 26.05.37020.3, libze1 1.27.0), the
torch build (`2.14.0+xpu`, the louise-python image's), the reproducer, and
for V1 the two numbers (204/512 → 512/512) with the texts' lengths.
