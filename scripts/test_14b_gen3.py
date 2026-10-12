#!/usr/bin/env python3
"""Test 3: replicate LlamaFactory's exact BNB load config + sanity prompts."""
import json
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import PeftModel

BASE = "/media/marcs/Linux_Apps/LLM/Qwen3_14b"
ADAPTER = "/media/marcs/06428AF2428AE5B1/AI Projects/Unsloth/training_outputs/qwen3_14b_swalt"

tok = AutoTokenizer.from_pretrained(BASE, trust_remote_code=True)
if tok.pad_token is None:
    tok.pad_token = tok.eos_token

# Exact LlamaFactory training load config
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
model = PeftModel.from_pretrained(model, ADAPTER)
model.eval()
print("loaded OK", flush=True)

def run(prompt, label):
    inputs = tok(prompt, return_tensors="pt").to("cuda:0")
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=120, do_sample=False,
                             eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id)
    text = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    print(f"===== {label} =====")
    print(text.strip()[:1000])
    print()

run("The capital of France is", "SANITY-en")
run("### Instruction:\nWrite a single voice-over line about Lara Croft.\n\n### Response:\n", "VO-lora")
with model.disable_adapter():
    inputs = tok("The capital of France is", return_tensors="pt").to("cuda:0")
    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=60, do_sample=False,
                             eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id)
    print("===== SANITY-base =====")
    print(tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()[:500])
print("DONE")
