#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import datetime as dt
import json
import re
import time
from pathlib import Path
from zoneinfo import ZoneInfo
from random import SystemRandom
from typing import Any, Optional

import requests

# --------- Settings ---------
LOCAL_TZ = ZoneInfo("Europe/Berlin")
POST_DIR = Path("content/posts")
DATA_DIR = Path("content/data")
CACHE_DIR = DATA_DIR / "cache"
SUM_CACHE = DATA_DIR / "summaries"
SEEN_PATH = DATA_DIR / "seen.json"

POST_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SUM_CACHE.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HiddenGemGamesBot/1.0 (+github actions)"})

rng = SystemRandom()

# Hidden-gem heuristics
GOOD_REVIEW_DESC = {"Very Positive", "Overwhelmingly Positive", "Mostly Positive", "Positive"}
MAX_PRICE_CENTS = 2500  # $25

# Speed toggle
FAST_MODE = True
FAST_TRIES = 40
FAST_PRICE_CENTS = 2500

# Rate limiting
MIN_INTERVAL_S = 0.45
_last_call = 0.0

# Content safety filters
ADULT_KEYWORDS = {"nudity","sexual","sex","adult","hentai","nsfw","porn","ecchi","erotic","lewd","fetish"}
JOKE_KEYWORDS = {"meme","joke","satire","parody","troll"}
NAME_BLOCKLIST = {"hentai","sex","nude","adult","nsfw","porn","strip","ecchi","erotic","boobs","yaoi","yuri","ahega","ahegal"}

# Hugging Face API (for summaries)
HF_TOKEN = os.getenv("HF_API_TOKEN")  # set in GitHub Actions secrets
HF_MODEL = os.getenv("HF_MODEL", "facebook/bart-large-cnn")  # solid summarizer on free tier
HF_API_URL = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
HF_HEADERS = {
    "Authorization": f"Bearer {HF_TOKEN}",
    "X-Wait-For-Model": "true",   # auto-spin the model if cold
} if HF_TOKEN else {}


# ---------- Utils ----------
def clean_text(s: str, max_len=240) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[: max_len - 1] + "…" if len(s) > max_len else s

def load_seen(max_keep=500):
    if SEEN_PATH.exists():
        try:
            data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
            ids = data.get("seen_appids", [])
            return ids[-max_keep:]
        except Exception:
            return []
    return []

def save_seen(seen):
    SEEN_PATH.write_text(json.dumps({"seen_appids": seen[-500:]}, indent=2), encoding="utf-8")


# ---------- HTTP + caching ----------
def _rate_limit():
    global _last_call
    now = time.monotonic()
    wait = _last_call + MIN_INTERVAL_S - now
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()

def http_get_json(url: str, *, params: dict | None = None, retries: int = 5, timeout: int = 30) -> Any:
    backoff = 0.7
    for attempt in range(retries):
        _rate_limit()
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code}", response=r)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(backoff)
                backoff *= 2.0
                continue
            raise

def get_applist():
    j = http_get_json("https://api.steampowered.com/ISteamApps/GetAppList/v2")
    return j.get("applist", {}).get("apps", [])

def get_appdetails(appid: int):
    cache_file = CACHE_DIR / f"app_{appid}.json"
    if cache_file.exists():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    j = http_get_json("https://store.steampowered.com/api/appdetails", params={"appids": appid})
    item = j.get(str(appid)) or {}
    data = item.get("data") if item.get("success") else None
    if data:
        cache_file.write_text(json.dumps(data), encoding="utf-8")
    return data

def get_review_summary(appid: int):
    j = http_get_json(
        f"https://store.steampowered.com/appreviews/{appid}",
        params={"json": 1, "language": "english", "purchase_type": "all", "filter": "summary", "num_per_page": 1},
    )
    return (j or {}).get("query_summary", {}) or {}

def fetch_review_texts(appid: int, num=40):
    url = f"https://store.steampowered.com/appreviews/{appid}"
    params = {
        "json": 1, "language": "english",
        "purchase_type": "all", "filter": "recent",
        "num_per_page": min(max(num, 5), 100)
    }
    r = SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json() or {}
    out = []
    for rv in (data.get("reviews") or []):
        txt = (rv.get("review") or "").strip()
        if txt:
            out.append(txt)
    return out


# ---------- Summarizer ----------
def hf_generate(prompt: str, max_new_tokens=160, temperature=0.2):
    """
    Call HF Inference API. Returns '' on any failure so the pipeline never blocks publishing.
    Works with both text-generation and summarization-style outputs.
    """
    if not HF_TOKEN:
        return ""  # no token => skip summaries

    payload = {
        "inputs": prompt,
        "parameters": {
            # Some models ignore these; harmless.
            "max_new_tokens": max_new_tokens,
            "temperature": temperature,
        }
    }

    try:
        r = requests.post(HF_API_URL, headers=HF_HEADERS, json=payload, timeout=60)
        # Common transient statuses: 503 while loading. We gracefully skip.
        if r.status_code in (503, 404):  # model loading or model not found
            return ""
        r.raise_for_status()
        out = r.json()

        # Summarization models (e.g., BART) often return dict or list with 'summary_text'
        if isinstance(out, list):
            # could be [{'summary_text': '...'}] OR [{'generated_text': '...'}]
            if out and isinstance(out[0], dict):
                return (out[0].get("summary_text")
                        or out[0].get("generated_text")
                        or "").strip()
        if isinstance(out, dict):
            return (out.get("summary_text")
                    or out.get("generated_text")
                    or "").strip()
        return ""
    except Exception:
        return ""  # never break the build

def chunk_texts(texts, max_chars=1800):
    chunks, buf = [], ""
    for t in texts:
        if len(buf) + len(t) + 1 > max_chars:
            if buf: chunks.append(buf); buf = ""
        buf += ("\n" + t) if buf else t
    if buf: chunks.append(buf)
    return chunks

def get_or_make_summary(appid: int):
    # If no token, skip LLM summaries entirely
    if not HF_TOKEN:
        return {"why": [], "likes": []}

    p = SUM_CACHE / f"{appid}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    reviews = fetch_review_texts(appid, num=30)
    if not reviews:
        return {"why": [], "likes": []}
    chunks = chunk_texts(reviews, max_chars=1800)

    bullets = {"why": [], "likes": []}
    for c in chunks:
        prompt = (
            "You are a neutral game critic. Summarize these Steam player reviews.\n"
            "Return plain text with two sections:\n"
            "Why: up to 3 short bullets\n"
            "Likes: up to 3 short bullets\n"
            f"REVIEWS:\n{c}"
        )
        out = hf_generate(prompt)
        for line in out.splitlines():
            if line.lower().startswith("why:") or line.startswith("-"):
                bullets["why"].append(line.strip("-• ").replace("Why:", "").strip())
            elif line.lower().startswith("likes:"):
                bullets["likes"].append(line.strip("-• ").replace("Likes:", "").strip())

    # dedupe & cap
    def uniq_cap(seq, n=5):
        seen = set(); out = []
        for s in seq:
            s = (s or "").strip(" •-").strip()
            if not s or s.lower() in seen: continue
            seen.add(s.lower()); out.append(s)
            if len(out) >= n: break
        return out

    final = {"why": uniq_cap(bullets["why"], 5), "likes": uniq_cap(bullets["likes"], 5)}
    p.write_text(json.dumps(final, indent=2), encoding="utf-8")
    return final


# ---------- Candidate checks ----------
def is_basic_candidate(data: dict) -> bool:
    if not data or data.get("type") != "game":
        return False
    if (data.get("release_date") or {}).get("coming_soon"):
        return False
    if not data.get("name") or not data.get("header_image"):
        return False
    langs = data.get("supported_languages") or ""
    if isinstance(langs, str) and "English" not in langs:
        return False
    is_free = data.get("is_free", False)
    price_cents = (data.get("price_overview") or {}).get("final")
    if not (is_free or (isinstance(price_cents, int) and price_cents <= FAST_PRICE_CENTS)):
        return False
    name = (data.get("name") or "").lower()
    if any(bad in name for bad in NAME_BLOCKLIST): return False
    for g in (data.get("genres") or []):
        if any(k in g.get("description","").lower() for k in ADULT_KEYWORDS): return False
    return True


# ---------- Picker ----------
def pick_game(apps, seen_set):
    attempts = 0
    while attempts < FAST_TRIES:
        attempts += 1
        app = rng.choice(apps)
        appid = app.get("appid")
        if not appid or appid in seen_set:
            continue
        try:
            data = get_appdetails(appid)
        except Exception:
            continue
        if not data: continue
        if is_basic_candidate(data):
            return appid, data
    return None, None


# ---------- Main ----------
def main():
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    slug_ts = now_local.strftime("%Y-%m-%d-%H%M%S")

    seen = load_seen()
    seen_set = set(seen)

    try:
        apps = get_applist()
    except Exception as e:
        print(f"[warn] GetAppList failed: {e}")
        apps = []

    appid, data = pick_game(apps, seen_set) if apps else (None, None)
    post_path = POST_DIR / f"{slug_ts}-auto.md"

    if not appid or not data:
        post_path.write_text(
            f"""Title: No Pick — {now_local.strftime('%Y-%m-%d %H:%M %Z')}
Date: {now_local.strftime('%Y-%m-%d %H:%M')}
Category: Games
Tags: auto
Slug: fallback-{slug_ts}

Could not fetch Steam data this run. Will try again next hour.
""",
            encoding="utf-8",
        )
        return

    name = data.get("name", f"App {appid}")
    short = clean_text(data.get("short_description", ""))
    header = data.get("header_image", "")
    release = (data.get("release_date") or {}).get("date", "—")
    genres = ", ".join([g.get("description") for g in (data.get("genres") or []) if g.get("description")][:5]) or "—"
    is_free = data.get("is_free", False)
    price = (data.get("price_overview") or {}).get("final_formatted")
    price_str = "Free to play" if is_free else (price or "Price varies")

    try:
        summary = get_review_summary(appid)
    except Exception:
        summary = {}
    desc = summary.get("review_score_desc")
    total = summary.get("total_reviews")

    # LLM review bullets
    summ = get_or_make_summary(appid)
    why_block = "\n\n### Why it’s a hidden gem\n" + "".join(f"- {w}\n" for w in summ.get("why", [])) if summ.get("why") else ""
    likes_block = "\n\n### What players like\n" + "".join(f"- {l}\n" for l in summ.get("likes", [])) if summ.get("likes") else ""

    md = f"""Title: {name}
Date: {now_local.strftime('%Y-%m-%d %H:%M')}
Category: Games
Tags: auto, steam
Slug: {appid}-{slug_ts}
Cover: {header}

![{name}]({header})

{short}

- Reviews: **{desc}**{f" ({total:,} total)" if total else "" if desc else ""}
- Release: **{release}**
- Genres: **{genres}**
- Price: **{price_str}**
- Steam AppID: `{appid}`

*Auto-generated; game chosen randomly each run, avoiding recent repeats.*{why_block}{likes_block}
"""

    post_path.write_text(md, encoding="utf-8")
    print(f"[ok] wrote {post_path} for {appid} — {name!r}")
    seen.append(appid)
    save_seen(seen)


if __name__ == "__main__":
    import os
    main()
