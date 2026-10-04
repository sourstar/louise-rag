"""A tiny MedCPT-shaped cross-encoder, for RAGE's smoke test of a Python
backend over LocalAI (deploy/rage/Dockerfile.localai, target `smoke`;
docs/notes/rage-over-localai.md §11): an OpenVINO model with MedCPT's three
inputs and a BERT pair tokenizer, which the louise-medcpt-rerank backend loads
on a CPU as it loads the real one. No torch.

Its logit is how often "whale" occurs in the DOCUMENT segment (token type 1,
unmasked), so a ranking can be checked by eye — and a backend that zeroed the
segment ids, the defect the real server exists to avoid, would score every
document 0.

    pip install openvino==2026.3.1 tokenizers==0.22.2   # for this tool only
    python tools/rage_smoke_reranker.py <models dir>/weights/medcpt/medcpt-cross-ov
"""
import sys
from pathlib import Path

import numpy as np
import openvino as ov
from openvino import opset13 as op
from tokenizers import Tokenizer, models, normalizers, pre_tokenizers, processors

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)

words = ["the", "a", "whale", "ship", "sea", "captain", "white", "hunt", "ahab", "moby", "dick"]
vocab = {t: i for i, t in enumerate(["[PAD]", "[UNK]", "[CLS]", "[SEP]", *words])}
tok = Tokenizer(models.WordPiece(vocab, unk_token="[UNK]"))
tok.normalizer = normalizers.BertNormalizer(lowercase=True)
tok.pre_tokenizer = pre_tokenizers.BertPreTokenizer()
tok.post_processor = processors.TemplateProcessing(
    single="[CLS] $A [SEP]", pair="[CLS] $A [SEP] $B:1 [SEP]:1",
    special_tokens=[("[CLS]", vocab["[CLS]"]), ("[SEP]", vocab["[SEP]"])])
tok.save(str(out / "tokenizer.json"))

table = np.zeros(len(vocab), dtype=np.float32)
table[vocab["whale"]] = 1.0
ids, mask, types = (op.parameter([-1, -1], ov.Type.i64, name=n)
                    for n in ("input_ids", "attention_mask", "token_type_ids"))
for p, n in zip((ids, mask, types), ("input_ids", "attention_mask", "token_type_ids")):
    p.output(0).set_names({n})
hits = op.gather(op.constant(table), ids, op.constant(np.int64(0)))
counted = op.multiply(op.multiply(hits, op.convert(types, ov.Type.f32)),
                      op.convert(mask, ov.Type.f32))
logits = op.reduce_sum(counted, op.constant(np.array([1], dtype=np.int64)), keep_dims=True)
logits.output(0).set_names({"logits"})
ov.save_model(ov.Model([logits], [ids, mask, types], "rage-smoke-cross-encoder"),
              str(out / "openvino_model.xml"))
print(f"{out}: openvino_model.xml, tokenizer.json")
