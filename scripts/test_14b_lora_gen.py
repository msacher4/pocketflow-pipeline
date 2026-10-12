#!/usr/bin/env python3
"""Compare base vs LoRA generations with the exact alpaca training template."""
import json
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from peft import PeftModel

BASE = "/media/marcs/Linux_Apps/LLM/Qwen3_14b"
ADAPTER = "/media/marcs/06428AF2428AE5B1/AI Projects/Unsloth/training_outputs/qwen3_14b_swalt"

tok = AutoTokenizer.from_pretrained(BASE, trust_remote_code=True)
if tok.pad_token is None:
    tok.pad_token = tok.eos_token

bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_compute_dtype=torch.bfloat16)
model = AutoModelForCausalLM.from_pretrained(
    BASE, quantization_config=bnb, device_map="auto", trust_remote_code=True
)
model = PeftModel.from_pretrained(model, ADAPTER)
model.eval()

val = [json.loads(l) for l in open(
    "/media/marcs/Linux_Apps/Projets_AI/pocketflow_pipeline/datasets/sw_alt_unsloth_val.jsonl")]
sample = val[0]
# Exact LlamaFactory alpaca user format used in training
prompt = "### Instruction:\n" + sample["instruction"] + "\n\n### Response:\n"
inputs = tok(prompt, return_tensors="pt").to(model.device)

BANNED = ("Video:", "Plan:", "Narration:", "SFX:", "Music:", "Action:")

def gen(label, disable=False):
    with torch.no_grad():
        if disable:
            with model.disable_adapter():
                out = model.generate(**inputs, max_new_tokens=250, do_sample=True,
                                     temperature=0.7, top_p=0.9,
                                     eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id)
        else:
            torch.manual_seed(42)
            out = model.generate(**inputs, max_new_tokens=250, do_sample=True,
                                 temperature=0.7, top_p=0.9,
                                 eos_token_id=tok.eos_token_id, pad_token_id=tok.pad_token_id)
    text = tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    lines = [l for l in text.strip().split("\n") if l.strip()]
    bad = [l for l in lines if l.strip().startswith(BANNED)]
    print(f"===== {label} ({len(lines)} lines, {len(bad)} banned) =====")
    print(text.strip()[:1600])
    print()

gen("LoRA", disable=False)
gen("BASE", disable=True)
print("EXPECTED (val response):")
print(sample["response"][:600])
