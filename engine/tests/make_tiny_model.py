"""Build a tiny random-weight GGUF (llama architecture, Qwen tokenizer, Qwen3.5 chat template) for engine tests.

It produces nonsense text, but exercises the real llama-server API: streaming, JSON-schema output, tool-call
parsing, timings and model switching. Needs llama.cpp's gguf-py on PYTHONPATH:
    PYTHONPATH=llama.cpp/gguf-py python make_tiny_model.py llama.cpp/models/ggml-vocab-qwen2.gguf template.jinja out.gguf
"""
import sys

import numpy as np
from gguf import GGUFReader, GGUFWriter, GGUFValueType


def main(vocab_path, template_path, out_path, n_embd=64, n_layer=2, n_head=4, n_ff=128):
    vocab = GGUFReader(vocab_path)
    w = GGUFWriter(out_path, "llama")
    w.add_name("tiny-test")
    w.add_context_length(4096)
    w.add_embedding_length(n_embd)
    w.add_block_count(n_layer)
    w.add_feed_forward_length(n_ff)
    w.add_rope_dimension_count(n_embd // n_head)
    w.add_head_count(n_head)
    w.add_head_count_kv(n_head)
    w.add_layer_norm_rms_eps(1e-6)
    w.add_file_type(1)
    n_vocab = None
    for field in vocab.fields.values():
        if not field.name.startswith("tokenizer."):
            continue
        if field.name == "tokenizer.chat_template":
            continue
        vtype = field.types[0]
        if vtype == GGUFValueType.ARRAY:
            sub = field.types[1]
            if sub == GGUFValueType.STRING:
                values = [bytes(field.parts[i]).decode("utf-8", errors="replace") for i in field.data]
            else:
                values = [field.parts[i].tolist()[0] for i in field.data]
            if field.name == "tokenizer.ggml.tokens":
                n_vocab = len(values)
            w.add_array(field.name, values)
        elif vtype == GGUFValueType.STRING:
            w.add_string(field.name, bytes(field.parts[field.data[0]]).decode("utf-8"))
        else:
            w.add_key_value(field.name, field.parts[field.data[0]].tolist()[0], vtype)
    w.add_chat_template(open(template_path, encoding="utf-8").read())
    rng = np.random.default_rng(0)

    def t(name, *shape):
        w.add_tensor(name, (rng.standard_normal(shape) * 0.02).astype(np.float16))

    t("token_embd.weight", n_vocab, n_embd)
    w.add_tensor("output_norm.weight", np.ones(n_embd, dtype=np.float32))
    t("output.weight", n_vocab, n_embd)
    for i in range(n_layer):
        w.add_tensor(f"blk.{i}.attn_norm.weight", np.ones(n_embd, dtype=np.float32))
        w.add_tensor(f"blk.{i}.ffn_norm.weight", np.ones(n_embd, dtype=np.float32))
        for name in ("attn_q", "attn_k", "attn_v", "attn_output"):
            t(f"blk.{i}.{name}.weight", n_embd, n_embd)
        t(f"blk.{i}.ffn_gate.weight", n_ff, n_embd)
        t(f"blk.{i}.ffn_up.weight", n_ff, n_embd)
        t(f"blk.{i}.ffn_down.weight", n_embd, n_ff)
    w.write_header_to_file()
    w.write_kv_data_to_file()
    w.write_tensors_to_file()
    w.close()


if __name__ == "__main__":
    main(*sys.argv[1:4])
