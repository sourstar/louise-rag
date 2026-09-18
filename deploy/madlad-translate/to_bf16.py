"""One-off: google/madlad400-3b-mt's float32 weights (11.8 GB) as bfloat16
(5.9 GB) — what the server loads anyway — so a start reads half as much and
leaves half the file cache. Checked: the same translation before and after."""
import sys, torch
from transformers import AutoTokenizer, T5ForConditionalGeneration

SRC, DST = "/data/madlad400-3b-mt", "/data/madlad400-3b-mt-bf16"
tok = AutoTokenizer.from_pretrained(SRC)
m = T5ForConditionalGeneration.from_pretrained(SRC, torch_dtype=torch.bfloat16,
                                               low_cpu_mem_usage=True).eval()
m.save_pretrained(DST, safe_serialization=True)
tok.save_pretrained(DST)
print("saved", flush=True)
text = ["<2de> The keeper lit the lamp every night.", "<2it> Call me Ishmael."]
outs = []
for path in (SRC, DST):
    mm = T5ForConditionalGeneration.from_pretrained(path, torch_dtype=torch.bfloat16,
                                                    low_cpu_mem_usage=True,
                                                    device_map="xpu").eval()
    enc = tok(text, return_tensors="pt", padding=True).to("xpu")
    with torch.inference_mode():
        out = tok.batch_decode(mm.generate(**enc, max_new_tokens=30), skip_special_tokens=True)
    print(path, out, flush=True)
    outs.append(out)
    del mm
    torch.xpu.empty_cache()
assert outs[0] == outs[1], "the bf16 copy translates differently"
print("identical", flush=True)
