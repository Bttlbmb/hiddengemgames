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
    Minimal Inference API call with retry/backoff for 429/5xx.
    """
    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": max_new_tokens,
            "temperature": temperature,
        }
    }
    backoff = 0.8
    for i in range(retries):
        r = requests.post(HF_API_URL, headers=HEADERS, json=payload, timeout=60)
        if r.status_code in (200, 201):
            out = r.json()
            # Inference API returns a list of dicts for text-generation
            if isinstance(out, list) and out and "generated_text" in out[0]:
                return out[0]["generated_text"].strip()
            # Some models return dict with 'summary_text'
            if isinstance(out, dict) and "summary_text" in out:
                return out["summary_text"].strip()
            return json.dumps(out)  # last resort
        if r.status_code in (429, 500, 502, 503, 504) and i < retries-1:
            time.sleep(backoff + uniform(0, 0.4))
            backoff *= 1.8
            continue
        r.raise_for_status()

def summarize_chunks(chunks):
    """
    Map-Reduce summarization:
      1) Summarize each chunk into JSON bullets
      2) Merge them into a final JSON
    Output: dict with keys why, likes (lists of short strings)
    """
    def map_prompt(text):
        return (
            "You are a neutral game critic. Summarize these player reviews.\n"
            "Return JSON with keys: why (max 5 bullets), likes (max 5 bullets).\n"
            "Each bullet must be <= 12 words. No commentary.\n"
            f"REVIEWS:\n{text}"
        )

    mapped = []
    for c in chunks:
        out = hf_generate(map_prompt(c), max_new_tokens=180)
        try:
            mapped.append(json.loads(out))
        except Exception:
            # fallback: treat raw text as one bullet if model didn't return JSON
            mapped.append({"why": [], "likes": [out[:120]]})

    # Reduce
    why, likes = [], []
    for m in mapped:
        why += (m.get("why") or [])
        likes += (m.get("likes") or [])

    # dedupe & cap
    def uniq_cap(seq, n=5):
        seen = set(); out = []
        for s in seq:
            s = (s or "").strip(" •-").strip()
            if not s or s.lower() in seen: continue
            seen.add(s.lower()); out.append(s)
            if len(out) >= n: break
        return out

    return {"why": uniq_cap(why, 5), "likes": uniq_cap(likes, 5)}
