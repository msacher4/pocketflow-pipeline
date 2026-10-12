#!/usr/bin/env python3
"""Test 4: norms to fp32 (like prepare_model_for_kbit_training) + generate."""
import json
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import PeftModel

BASE = "/media/marcs/Linux_Apps/LLM/Qwen3_14b"
ADAPTER = "/media/marcs/06428AF2428AE5B1/AI Projects/Unsloth/training_outputs/qwen3_14b_swalt"

tok = AutoTokenizer.from_pretrained(BASE, trust_remote_code=True)
if tok.pad_token is None:
    tok.pad_token = tok.eos_token

bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_quant_storage=torch.bfloat16,
)
model = AutoModelForCausalLM.from_pretrained(
    BASE, quantization_config=bnb, device_map={"": 0},
    trust_remote_code=True, torch_dtype=torch.bfloat16,
)
# replicate prepare_model_for_kbit_training: norms to fp32
n_cast = 0
for n, m in model.named_modules():
    if "norm" in n and isinstance(m, torch.nn.Module):
        for p in m.parameters(recurse=False):
            if p.dtype in (torch.bfloat16, torch.float16):
                p.data = p.data.to(torch.float32)
                n_cast += 1
print(f"norms cast to fp32: {n_cast}", flush=True)

model = PeftModel.from_pretrained(model, ADAPTER)
model.eval()

def run(prompt, label, use_adapter=True):
    inputs = tok(prompt, return_tensors="pt").to("cuda:0")
    with torch.no_grad():
        if use_adapter:
            out = model.generate(**inputs, max_new_tokens=100, do_sample=False,
                                 eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id)
        else:
            with model.disable_adapter():
                out = model.generate(**inputs, max_new_tokens=100, do_sample=False,
                                     eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id)
    text = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    print(f"===== {label} =====")
    print(text.strip()[:800])
    print()

run("The capital of France is", "SANITY-lora")
run("The capital of France is", "SANITY-base", use_adapter=False)
print("DONE")
