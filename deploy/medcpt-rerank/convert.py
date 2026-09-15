"""MedCPT-Cross-Encoder -> OpenVINO IR (fp16 weights), and the reference to
hold it to: the model card's own usage (transformers, fp32, CPU) on real
pubmed candidates (/in.json: [{query, documents}]). Then the IR on CPU
against it, fed by the sidecar's tokenizer path (tokenizers + tokenizer.json),
which must reproduce transformers' encodings exactly — segment ids above all."""
import itertools
import json
import os
import time

import numpy as np
import openvino as ov
import tokenizers
import torch
import transformers
from huggingface_hub import snapshot_download
from transformers import AutoModelForSequenceClassification, AutoTokenizer

OUT = "/models/medcpt-cross-ov"
os.makedirs(OUT, exist_ok=True)
print("openvino", ov.__version__, "| tokenizers", tokenizers.__version__,
      "| transformers", transformers.__version__, "| torch", torch.__version__)

path = snapshot_download("ncbi/MedCPT-Cross-Encoder", cache_dir="/models/hf")
tok = AutoTokenizer.from_pretrained(path)
model = AutoModelForSequenceClassification.from_pretrained(path).eval()
print("labels:", model.config.num_labels, "| type_vocab:", model.config.type_vocab_size,
      "| max_pos:", model.config.max_position_embeddings)


class Logits(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, input_ids, attention_mask, token_type_ids):
        return self.m(input_ids=input_ids, attention_mask=attention_mask,
                      token_type_ids=token_type_ids).logits


NAMES = ("input_ids", "attention_mask", "token_type_ids")
ex = tok([["a query", "an article about it"]], padding=True, truncation=True,
         max_length=512, return_tensors="pt")
ovm = ov.convert_model(Logits(model).eval(), example_input={k: ex[k] for k in NAMES})
# the converter can rename an input after an internal tensor ("61" for the
# mask); the forward signature fixes the order, so name them by position
for port, name in zip(ovm.inputs, NAMES):
    port.get_tensor().set_names({name})
ovm.reshape({name: ov.PartialShape([-1, -1]) for name in NAMES})
print("IR inputs:", [(i.get_any_name(), str(i.get_partial_shape())) for i in ovm.inputs],
      "| output:", str(ovm.outputs[0].get_partial_shape()))
ov.save_model(ovm, f"{OUT}/openvino_model.xml", compress_to_fp16=True)
tok.save_pretrained(OUT)

data = json.load(open("/in.json"))
fast = tokenizers.Tokenizer.from_file(f"{OUT}/tokenizer.json")
fast.enable_truncation(512)
fast.enable_padding()
# f32 on purpose: this CPU (Zen 4, AVX-512 BF16) would otherwise run bf16,
# and the point here is the conversion, not a precision
compiled = ov.Core().compile_model(f"{OUT}/openvino_model.xml", "CPU",
                                   {"INFERENCE_PRECISION_HINT": "f32"})


def tau(a, b):
    ps = list(itertools.combinations(range(len(a)), 2))
    return sum(1 if (a[i] - a[j]) * (b[i] - b[j]) > 0 else -1 for i, j in ps) / len(ps)


ref = []
for item in data:
    q, docs = item["query"], item["documents"]
    pairs = [[q, d] for d in docs]
    t0 = time.time()
    with torch.no_grad():
        x = tok(pairs, padding=True, truncation=True, max_length=512, return_tensors="pt")
        r = model(**x).logits.view(-1).float().tolist()
    t_ref = time.time() - t0
    encs = fast.encode_batch([(q, d) for d in docs])
    mine = {"input_ids": np.array([e.ids for e in encs], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64)}
    same = all(np.array_equal(mine[k], x[k].numpy()) for k in NAMES)
    seg1 = int(mine["token_type_ids"].sum())
    t0 = time.time()
    o = next(iter(compiled.infer_new_request(mine).values())).reshape(-1).tolist()
    t_ov = time.time() - t0
    diff = max(abs(a - b) for a, b in zip(o, r))
    print(f"{q[:40]!r}: {len(docs)} docs, tokens {int(mine['attention_mask'].sum())} "
          f"(segment-1 {seg1}) | encodings identical: {same} | IR-CPU vs ref: max |diff| "
          f"{diff:.4f}, tau {tau(o, r):.3f}, top-5 same {sorted(range(len(r)), key=lambda i: -r[i])[:5] == sorted(range(len(o)), key=lambda i: -o[i])[:5]} "
          f"| ref {t_ref:.1f}s, IR-CPU {t_ov:.1f}s")
    ref.append({"query": q, "scores": r})
json.dump(ref, open(f"{OUT}/reference-scores.json", "w"))
print("wrote", sorted(os.listdir(OUT)))
