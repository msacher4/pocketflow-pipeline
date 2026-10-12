#!/usr/bin/env python3
from unsloth import FastLanguageModel
import torch
from datasets import load_dataset

model, tokenizer = FastLanguageModel.from_pretrained(
    model_name="unsloth/Qwen3.8-27B-unsloth-bnb-4bit",
    max_seq_length=2048,
    dtype=None,
    load_in_4bit=True,
    trust_remote_code=True,
    device_map="auto",
)

model = FastLanguageModel.get_peft_model(
    model,
    r=16,
    lora_alpha=32,
    target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
    lora_dropout=0.05,
    bias="none",
    use_gradient_checkpointing="unsloth",
    random_state=42,
)

dataset = load_dataset("json", data_files={
    "train": "/media/marcs/Linux_Apps/Projets_AI/pocketflow_pipeline/datasets/sw_alt_unsloth_train.jsonl",
    "val":   "/media/marcs/Linux_Apps/Projets_AI/pocketflow_pipeline/datasets/sw_alt_unsloth_val.jsonl"
})

EOS = tokenizer.eos_token
def formatting_prompts_func(examples):
    texts = []
    for instr, resp in zip(examples["instruction"], examples["response"]):
        texts.append(f"{instr}\n\n{resp}{EOS}")
    return {"text": texts}

dataset = dataset.map(formatting_prompts_func, batched=True, remove_columns=["instruction","response"])

from trl import SFTTrainer
from transformers import TrainingArguments

trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset["train"],
    eval_dataset=dataset["val"] if len(dataset["val"]) > 0 else None,
    dataset_text_field="text",
    max_seq_length=2048,
    packing=False,
    args=TrainingArguments(
        per_device_train_batch_size=1,
        gradient_accumulation_steps=8,
        warmup_steps=5,
        num_train_epochs=5,
        learning_rate=2e-4,
        fp16=True,
        logging_steps=1,
        optim="adamw_torch",
        weight_decay=0.01,
        lr_scheduler_type="linear",
        seed=42,
        output_dir="/media/marcs/06428AF2428AE5B1/AI Projects/Unsloth/training_outputs/qwen3_8_27b_qlora",
        save_strategy="steps",
        save_steps=10,
        eval_strategy="steps" if dataset["val"] else "no",
        eval_steps=10 if dataset["val"] else None,
        report_to="none",
    ),
)

trainer.train()
model.save_pretrained("/media/marcs/06428AF2428AE5B1/AI Projects/Unsloth/trained/qwen3_8_27b_swalt")
tokenizer.save_pretrained("/media/marcs/06428AF2428AE5B1/AI Projects/Unsloth/trained/qwen3_8_27b_swalt")
