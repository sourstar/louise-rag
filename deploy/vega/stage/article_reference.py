"""Switch-over step 3 (docs/notes/vega.md §12): MedCPT's article encoder as
NCBI uses it (transformers, fp32, CPU; the tokenizer's own pair encoding cut
longest-first to 512; the [CLS] row of the last hidden state) on the pairs of
/in.json, written as little-endian f32 in base64 to /out.json. The reference
for `vega-medcpt-article-ov`, which has no counterpart in RAGE. Runs in
convert_article.sh's container, after the conversion, fetching nothing.
"""
import base64
import json
import struct
import sys

import torch
from huggingface_hub import snapshot_download
from transformers import AutoModel, AutoTokenizer

path = snapshot_download("ncbi/MedCPT-Article-Encoder", cache_dir="/models",
                         local_files_only=True)
tok = AutoTokenizer.from_pretrained(path)
model = AutoModel.from_pretrained(path).eval()
pairs = json.load(open(sys.argv[1]))
out = []
with torch.no_grad():
    for title, abstract in pairs:
        x = tok([[title, abstract]], truncation=True, padding=True, max_length=512,
                return_tensors="pt")
        v = model(**x).last_hidden_state[:, 0, :][0].tolist()
        out.append(base64.b64encode(struct.pack(f"<{len(v)}f", *v)).decode())
json.dump({"snapshot": path, "vectors": out}, open(sys.argv[2], "w"))
print("reference vectors", len(out), "dim", len(v), "snapshot", path)
