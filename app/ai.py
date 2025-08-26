# app/ai.py
"""
LLM helpers for Hidden Gem Games — Cloudflare Workers AI edition.
Produces short, connected prose (no bullet points) with a light editorial tone.
"""

from __future__ import annotations

import os
import json
import time
from typing import Optional, Dict, Any

import requests

# Default model (cheap + solid). You can swap to @cf/meta/llama-3.1-8b-instruct too.
DEFAULT_MODEL = os.environ.get("HGG_AI_MODEL", "@cf/meta/llama-3-8b-instruct")

CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_API_TOKEN  = os.environ.get("CF_API_TOKEN", "")
CF_TIMEOUT    = float(os.environ.get("HGG_AI_TIMEOUT", "20"))
CF_RETRIES    = int(os.environ.get("HGG_AI_RETRIES", "2"))  # minimal retries to save quota


def _cf_url(model: str) -> str:
    # REST inference endpoint
    return f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{model.lstrip('@')}"


def cf_generate(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 220,
    temperature: float = 0.7,
    system: Optional[str] = None,
) -> str:
    """
    Single call to Cloudflare Workers AI. Returns text or raises for hard errors.
    We keep this lean to conserve 'neurons'.
    """
    if not CF_ACCOUNT_ID or not CF_API_TOKEN:
        raise RuntimeError("Cloudflare Workers AI credentials missing (CF_ACCOUNT_ID / CF_API_TOKEN)")

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
    }

    payload: Dict[str, Any] = {
        "messages": [],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if system:
        payload["messages"].append({"role": "system", "content": system})
    payload["messages"].append({"role": "user", "content": prompt})

    url = _cf_url(model)

    last_err = None
    for attempt in range(CF_RETRIES + 1):
        try:
            r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=CF_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            # Workers AI unifies outputs under result.response
            txt = (
                data.get("result", {})
                    .get("response", "")
                    .strip()
            )
            if not txt:
                # Some models respond in OpenAI-ish shape; play nice:
                choices = data.get("result", {}).get("choices") or []
                if choices and "text" in choices[0]:
                    txt = (choices[0]["text"] or "").strip()
            if txt:
                return txt
            last_err = RuntimeError("Empty response text")
        except Exception as e:
            last_err = e
            # brief backoff to avoid spiky 429s
            time.sleep(0.8 + attempt * 0.6)
    raise last_err or RuntimeError("Workers AI call failed")


# ---------- Task-specific generators (prose, no lists) ----------

SYS_NEUTRAL = (
    "You are an assistant that writes compact, connected editorial prose for a games website. "
    "Use plain, neutral language; avoid marketing fluff and avoid bullet points."
)

def build_corpus(short_description: str, reviews_sample: str) -> str:
    # Short, bounded input to save tokens
    intro = f"Description:\n{short_description.strip()}\n"
    if reviews_sample:
        intro += "\nSelected Review Snippets:\n" + reviews_sample.strip()
    return intro


def make_overview_text(corpus: str) -> str:
    prompt = (
        "Write a 2–4 sentence neutral overview of the game based on the provided description and review snippets. "
        "Explain what you do in the game and its key mechanics without hype. "
        "Avoid bullet points."
        "\n\n=== INPUT ===\n"
        f"{corpus}\n"
        "=== END ==="
    )
    return cf_generate(prompt, system=SYS_NEUTRAL, max_tokens=220, temperature=0.5)


def make_hidden_gem_text(corpus: str) -> str:
    prompt = (
        "In 1–2 sentences, explain *why this could be a hidden gem* for some players, "
        "focusing on specific qualities (mechanics, vibe, art, depth, or uniqueness). "
        "Avoid bullet points and marketing tone."
        "\n\n=== INPUT ===\n"
        f"{corpus}\n"
        "=== END ==="
    )
    return cf_generate(prompt, system=SYS_NEUTRAL, max_tokens=140, temperature=0.6)


def make_likes_text(corpus: str) -> str:
    prompt = (
        "Summarize in 2–3 sentences what players like about this game. "
        "Write connected editorial prose (no lists, no 'some players say'). "
        "Be specific but concise."
        "\n\n=== INPUT ===\n"
        f"{corpus}\n"
        "=== END ==="
    )
    return cf_generate(prompt, system=SYS_NEUTRAL, max_tokens=160, temperature=0.6)


def make_dislikes_text(corpus: str) -> str:
    prompt = (
        "Summarize in 2–3 sentences what players criticize about this game. "
        "Write connected editorial prose (no lists, no 'some players say'). "
        "Be specific but concise."
        "\n\n=== INPUT ===\n"
        f"{corpus}\n"
        "=== END ==="
    )
    return cf_generate(prompt, system=SYS_NEUTRAL, max_tokens=160, temperature=0.6)
