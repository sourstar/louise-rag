"""A tiny llama-architecture GGUF with random weights, for RAGE's smoke test
over LocalAI (deploy/rage/Dockerfile.localai, target `smoke`;
docs/notes/rage-over-localai.md §11): LocalAI loads it on a CPU, embeds with
it, and RAGE states the run. Its vectors mean nothing; 480 KB.

    pip install gguf==0.17.1          # for this tool only, not a dependency of Louise
    python tools/rage_smoke_model.py <models dir>/weights/tiny/tiny.gguf
"""
import sys

import numpy as np
import gguf

out = sys.argv[1]
n_vocab, n_embd, n_ff, n_layer, n_head, n_ctx = 300, 64, 128, 2, 4, 512
rng = np.random.default_rng(42)

w = gguf.GGUFWriter(out, "llama")
w.add_name("rage-smoke-tiny")
w.add_context_length(n_ctx)
w.add_embedding_length(n_embd)
w.add_block_count(n_layer)
w.add_feed_forward_length(n_ff)
w.add_head_count(n_head)
w.add_head_count_kv(n_head)
w.add_rope_dimension_count(n_embd // n_head)
w.add_layer_norm_rms_eps(1e-5)
w.add_file_type(gguf.LlamaFileType.ALL_F32)

# a SentencePiece vocabulary: the three specials, the 256 byte tokens the
# byte fallback needs, and a few word pieces
tokens = [b"<unk>", b"<s>", b"</s>"] + [f"<0x{i:02X}>".encode() for i in range(256)]
pieces = ["▁" + c for c in "abcdefghijklmnopqrstuvwxyz"] + list("abcdefghijklmn")
tokens += [p.encode() for p in pieces]
tokens = tokens[:n_vocab] + [f"<extra{i}>".encode() for i in range(n_vocab - len(tokens))]
types = ([gguf.TokenType.UNKNOWN, gguf.TokenType.CONTROL, gguf.TokenType.CONTROL]
         + [gguf.TokenType.BYTE] * 256
         + [gguf.TokenType.NORMAL] * (n_vocab - 259))
w.add_tokenizer_model("llama")
w.add_token_list(tokens)
w.add_token_scores([0.0] * 3 + [0.0] * 256 + [-float(i) for i in range(n_vocab - 259)])
w.add_token_types(types)
w.add_bos_token_id(1)
w.add_eos_token_id(2)
w.add_unk_token_id(0)


def t(name, *shape):
    w.add_tensor(name, (rng.standard_normal(shape) * 0.02).astype(np.float32))


t("token_embd.weight", n_vocab, n_embd)
w.add_tensor("output_norm.weight", np.ones(n_embd, dtype=np.float32))
t("output.weight", n_vocab, n_embd)
for i in range(n_layer):
    w.add_tensor(f"blk.{i}.attn_norm.weight", np.ones(n_embd, dtype=np.float32))
    t(f"blk.{i}.attn_q.weight", n_embd, n_embd)
    t(f"blk.{i}.attn_k.weight", n_embd, n_embd)
    t(f"blk.{i}.attn_v.weight", n_embd, n_embd)
    t(f"blk.{i}.attn_output.weight", n_embd, n_embd)
    w.add_tensor(f"blk.{i}.ffn_norm.weight", np.ones(n_embd, dtype=np.float32))
    t(f"blk.{i}.ffn_gate.weight", n_ff, n_embd)
    t(f"blk.{i}.ffn_up.weight", n_ff, n_embd)
    t(f"blk.{i}.ffn_down.weight", n_embd, n_ff)

w.write_header_to_file()
w.write_kv_data_to_file()
w.write_tensors_to_file()
w.close()
print(out)
