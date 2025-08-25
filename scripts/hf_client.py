#!/usr/bin/env python3
import os, time, json, requests
from pathlib import Path
from random import uniform

HF_TOKEN = os.environ.get("HF_API_TOKEN")  # injected by GitHub Actions
HF_API_URL = "https://api-inference.huggingface.co/models/google/flan-t5-small"
# You can swap to a bigger free model later:
#   - "facebook/bart-large-cnn" (summarization)
#   - "mistralai/Mistral-7B-Instruct-v0.2" (general chat)
# Some community models may cold-start; FLAN-T5 Small is a safe starter.

HEADERS = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

def hf_generate(prompt: str, max_new_tokens=160, temperature=0.2, retries=5):
    """
    Minimal Inference API call with retry/backoff for 
