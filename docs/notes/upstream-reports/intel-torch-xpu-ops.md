# intel/torch-xpu-ops — `torch.softmax` (fp32) returns wrong values for row lengths 513–768, 1025–1280, 1537–1792

**Where:** torch 2.14.0+xpu and 2.14.1+xpu, Intel Arc Pro B70 (Battlemage, `xe`
driver, Linux 6.18).

**Root cause, as observed:**
- `torch.softmax(x, dim=-1)` on a float32 XPU tensor is wrong whenever the
  last dimension L satisfies `L > 512 and (L - 1) % 512 < 256`.
- The values are finite but exceed 1 (up to ~18). fp16 and bf16 are correct,
  and `scaled_dot_product_attention` is correct at the same lengths.
- It is deterministic: the same wrong output on every run.

**Reproducer:**

```python
import torch
for L in (512, 513, 597, 768, 769, 1100, 1600):
    x = torch.randn(1, 1, L, L) * 4
    err = (torch.softmax(x, -1) - torch.softmax(x.to("xpu"), -1).cpu()).abs().max()
    print(L, float(err))   # B70: ~1e-7 at 512, 769; ~1-18 at 513, 597, 768, 1100, 1600
```

A full sweep of L = 1..2048 fails exactly on 513–768, 1025–1280 and
1537–1792. 4-D (1, 16, L, L) and 3-D (16, L, L) tensors fail too.

**Impact:** a transformer with eager fp32 attention silently produces
unrelated embeddings for inputs of those lengths. Seen with
jinaai/jina-embeddings-v3: cosine −0.07 to the CPU's vector for a 597-token
text.

**Workaround used:** route attention through
`F.scaled_dot_product_attention`. Its vectors match the CPU's within 1e-4.
