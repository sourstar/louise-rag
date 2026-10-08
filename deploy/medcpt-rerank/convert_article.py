"""MedCPT-Article-Encoder -> OpenVINO IR, the weights of louise-medcpt-article-ov
(docs/notes/input-forms.md; convert.py is the cross-encoder's). Then the IR on
CPU held to NCBI's own usage (transformers, fp32, CPU) on text pairs, fed by
the backend's tokenizer path (server.make_encoder), which must reproduce
transformers' encodings exactly — segment ids above all.

    docker run --rm -e HF_HUB_OFFLINE=1 \\
      -v /mnt/ssd5/apps/medcpt-models:/models \\
      -v "$PWD/deploy/medcpt-rerank:/w:ro" python:3.12-slim sh -c '
        pip install -q torch --index-url https://download.pytorch.org/whl/cpu &&
        pip install -q transformers openvino==2026.3.1 tokenizers==0.22.2 numpy &&
        python /w/convert_article.py [/in.json]'

Reads the weights already on disk (the Hugging Face cache under /models, as the
node holds it: models--ncbi--MedCPT-Article-Encoder); fetches nothing. Writes
/models/medcpt-article-ov: openvino_model.xml/.bin, whose output is the LAST
HIDDEN STATE (the backend takes its [CLS] row, server.make_cls_infer), and the
tokenizer files. Weights stay fp32: OpenVINO's GPU plugin picks its own
inference precision, and the IR adds no second rounding to it.

Optional /in.json: [[title, abstract], …] — real pubmed records to check on;
without it a handful of built-in pairs, one past 512 tokens.
"""
import json
import os
import sys

import numpy as np
import openvino as ov
import torch
from huggingface_hub import snapshot_download
from transformers import AutoModel, AutoTokenizer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402 — the backend's model code, beside this file

OUT = "/models/medcpt-article-ov"
NAMES = server.INPUTS
MAX = server.MAX_TOKENS
#: the IR on CPU against transformers: what engine noise alone leaves
MIN_COSINE = 0.99999

path = snapshot_download("ncbi/MedCPT-Article-Encoder", cache_dir="/models",
                         local_files_only=True)
tok = AutoTokenizer.from_pretrained(path)
model = AutoModel.from_pretrained(path).eval()
print("openvino", ov.__version__, "| torch", torch.__version__, "| snapshot", path)


class Hidden(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, input_ids, attention_mask, token_type_ids):
        return self.m(input_ids=input_ids, attention_mask=attention_mask,
                      token_type_ids=token_type_ids).last_hidden_state


ex = tok([["a title", "an abstract about it"]], padding=True, truncation=True,
         max_length=MAX, return_tensors="pt")
ovm = ov.convert_model(Hidden(model).eval(), example_input={k: ex[k] for k in NAMES})
for port, name in zip(ovm.inputs, NAMES):       # named by position, as convert.py
    port.get_tensor().set_names({name})
ovm.reshape({name: ov.PartialShape([-1, -1]) for name in NAMES})
os.makedirs(OUT, exist_ok=True)
ov.save_model(ovm, f"{OUT}/openvino_model.xml", compress_to_fp16=False)
tok.save_pretrained(OUT)
print("IR inputs:", [(i.get_any_name(), str(i.get_partial_shape())) for i in ovm.inputs],
      "| output:", str(ovm.outputs[0].get_partial_shape()))

if len(sys.argv) > 1:
    pairs = [tuple(p) for p in json.load(open(sys.argv[1]))]
else:
    pairs = [("Aspirin and colorectal cancer.", "We studied aspirin use in a cohort."),
             ("Title only, empty abstract", ""),
             ("A long one", " ".join(["mitochondrial dysfunction in neurons"] * 200))]
encode = server.make_encoder(f"{OUT}/tokenizer.json", MAX)
# THE IR IS HELD TO THE MODEL IN f32. OpenVINO's CPU plugin infers in bf16 by
# default on a CPU that has it (the node's Ryzen 7600: avx512_bf16), which
# alone gave cosines of 0.99995-0.99999 against transformers' fp32 — a
# precision the plugin chose, not the IR (node, 2026-10-08, vega.md §12). The
# backend's own path (server.make_cls_infer, the plugin's default precision)
# is reported beside it, never held to MIN_COSINE.
_f32 = ov.Core().compile_model(f"{OUT}/openvino_model.xml", "CPU",
                               {"INFERENCE_PRECISION_HINT": "f32"})


def infer(inputs):
    return [row for row in next(iter(_f32.infer_new_request(inputs).values()))[:, 0, :]]


infer_default = server.make_cls_infer(f"{OUT}/openvino_model.xml", "CPU")
worst = worst_default = 1.0
for title, abstract in pairs:
    with torch.no_grad():
        x = tok([[title, abstract]], truncation=True, padding=True, max_length=MAX,
                return_tensors="pt")
        ref = model(**x).last_hidden_state[:, 0, :][0].numpy()
    mine = encode([(title, abstract)])
    same = all(np.array_equal(mine[k], x[k].numpy()) for k in NAMES)
    (vec,) = infer(mine)
    vec = np.asarray(vec)
    cos = float(vec @ ref / (np.linalg.norm(vec) * np.linalg.norm(ref)))
    worst = min(worst, cos)
    (vd,) = infer_default(mine)
    vd = np.asarray(vd)
    cos_default = float(vd @ ref / (np.linalg.norm(vd) * np.linalg.norm(ref)))
    worst_default = min(worst_default, cos_default)
    print(f"{title[:40]!r}: tokens {int(mine['attention_mask'].sum())} (segment-1 "
          f"{int(mine['token_type_ids'].sum())}) | encodings identical: {same} | "
          f"cosine IR-CPU f32 vs transformers {cos:.7f} | at the plugin's default "
          f"precision {cos_default:.6f}")
    if not same:
        sys.exit("the backend's tokenizer path does not reproduce transformers' encoding")
print(f"worst cosine: f32 {worst:.7f} (held to {MIN_COSINE}); the plugin's default "
      f"precision {worst_default:.6f} (reported)")
if worst < MIN_COSINE:
    sys.exit(f"worst cosine {worst:.6f} < {MIN_COSINE}: the IR is not the model")
print("wrote", sorted(os.listdir(OUT)))
