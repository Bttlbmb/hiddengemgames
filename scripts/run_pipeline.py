#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
from random import SystemRandom
from typing import Any, Optional

import requests

# =========================
# Paths & global setup
# =========================
LOCAL_TZ = ZoneInfo("Europe/Berlin")
POST_DIR  = Path("content/posts")
DATA_DIR  = Path("content/data")
CACHE_DIR = DATA_DIR / "cache"
SUM_CACHE = DATA_DIR / "summaries"
SEEN_PATH = DATA_DIR / "seen.json"

for p in (POST_DIR, DATA_DIR, CACHE_DIR, SUM_CACHE):
    p.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HiddenGemGamesBot/1.0 (+github actions)"})
rng = SystemRandom()

# =========================
# Picker heuristics
# =========================
GOOD_REVIEW_DESC = {"Very Positive", "Overwhelmingly Positive", "Mostly Positive", "Positive"}
FAST_MODE = True
FAST_TRIES = 40
FAST_PRICE_CENTS = 2500  # $25 ceiling in fast mode

# content-safety filters
ADULT_KEYWORDS = {"nudity","sexual","sex","adult","hentai","nsfw","porn","ecchi","erotic","lewd","fetish"}
JOKE_KEYWORDS  = {"meme","joke","satire","parody","troll"}
NAME_BLOCKLIST = {"hentai","sex","nude","adult","nsfw","porn","strip","ecchi","erotic","boobs","yaoi","yuri","ahega","ahegal"}

# rate limit for Steam calls
MIN_INTERVAL_S = 0.45
_last_call = 0.0

# =========================
# Helpers
# =========================
def clean_text(s: str, max_len=240) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[: max_len - 1] + "…" if len(s) > max_len else s

def load_seen(max_keep=500):
    if SEEN_PATH.exists():
        try:
            data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
            return (data.get("seen_appids") or [])[-max_keep:]
        except Exception:
            return []
    return []

def save_seen(seen):
    SEEN_PATH.write_text(json.dumps({"seen_appids": seen[-500:]}, indent=2), encoding="utf-8")

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

# =========================
# Steam fetchers (with cache)
# =========================
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

def fetch_review_texts(appid: int, num=30):
    url = f"https://store.steampowered.com/appreviews/{appid}"
    params = {
        "json": 1, "language": "english",
        "purchase_type": "all", "filter": "recent",
        "num_per_page": min(max(num, 5), 100),
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

# =========================
# Content-safety
# =========================
def is_basic_candidate(data: dict) -> bool:
    if not data or data.get("type") != "game":
        return False
    if (data.get("release_date") or {}).get("coming_soon"):
        return False
    if not data.get("name") or not data.get("header_image"):
        return False

    # English support?
    langs = data.get("supported_languages") or ""
    if isinstance(langs, str) and "English" not in langs:
        return False

    # Price gate (fast mode)
    is_free = data.get("is_free", False)
    price_cents = (data.get("price_overview") or {}).get("final")
    if not (is_free or (isinstance(price_cents, int) and price_cents <= FAST_PRICE_CENTS)):
        return False

    # Safety: names/genres
    name_low = (data.get("name") or "").lower()
    if any(b in name_low for b in NAME_BLOCKLIST):
        return False
    for g in (data.get("genres") or []):
        desc = (g.get("description") or "").lower()
        if any(k in desc for k in ADULT_KEYWORDS | JOKE_KEYWORDS):
            return False

    # quick exclude by name tail
    if any(t in name_low for t in ("demo", "soundtrack", "ost", "dlc", "server")):
        return False

    return True

# =========================
# Picker
# =========================
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
        if not data:
            continue
        if is_basic_candidate(data):
            return appid, data
    return None, None

# =========================
# Hugging Face summaries
# =========================
HF_TOKEN = os.getenv("HF_API_TOKEN")
HF_MODEL = os.getenv("HF_MODEL", "facebook/bart-large-cnn")  # default: reliable summarizer
HF_API_URL = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
HF_HEADERS = {
    "Authorization": f"Bearer {HF_TOKEN}",
    "X-Wait-For-Model": "true",
} if HF_TOKEN else {}

def hf_generate(prompt: str, max_new_tokens=200, temperature=0.2) -> str:
    """Call HF Inference API. Returns '' on failure so build never blocks."""
    if not HF_TOKEN:
        return ""
    payload = {
        "inputs": prompt,
        "parameters": {"max_new_tokens": max_new_tokens, "temperature": temperature},
    }
    try:
        r = requests.post(HF_API_URL, headers=HF_HEADERS, json=payload, timeout=90)
        if r.status_code in (503, 404):
            return ""
        r.raise_for_status()
        out = r.json()

        print("[hf raw]", out)
        
        if isinstance(out, list) and out and isinstance(out[0], dict):
            return (out[0].get("summary_text") or out[0].get("generated_text") or "").strip()
        if isinstance(out, dict):
            return (out.get("summary_text") or out.get("generated_text") or "").strip()
        return ""
    except Exception:
        return ""

def _uniq_cap(seq, n=5):
    seen = set(); out = []
    for s in seq:
        s = (s or "").strip(" •-").strip()
        if not s: continue
        low = s.lower()
        if low in seen: continue
        seen.add(low); out.append(s)
        if len(out) >= n: break
    return out

def _parse_json_like(text: str):
    import json as _json
    try:
        obj = _json.loads(text)
        why = obj.get("why") or obj.get("pros") or []
        likes = obj.get("likes") or obj.get("what_players_like") or obj.get("cons") or []
        why = [str(x) for x in (why if isinstance(why, list) else [why])]
        likes = [str(x) for x in (likes if isinstance(likes, list) else [likes])]
        return _uniq_cap(why, 5), _uniq_cap(likes, 5)
    except Exception:
        return [], []

def _parse_two_lines(text: str):
    why, likes = [], []
    for line in text.splitlines():
        low = line.lower()
        if low.startswith("why:"):
            payload = line.split(":", 1)[1]
            why = [p.strip() for p in payload.split(";") if p.strip()]
        elif low.startswith("likes:"):
            payload = line.split(":", 1)[1]
            likes = [p.strip() for p in payload.split(";") if p.strip()]
    return _uniq_cap(why, 5), _uniq_cap(likes, 5)

def _fallback_from_metadata(short_desc: str, review_desc: Optional[str], total: Optional[int]):
    why = []
    if short_desc:
        sentences = re.split(r'[.!?]+', short_desc)
        for s in sentences:
            s = s.strip()
            if 6 <= len(s) <= 100:
                why.append(s)
            if len(why) >= 3:
                break
    likes = []
    if review_desc:
        likes.append(f"{review_desc} overall sentiment")
    if total:
        likes.append(f"{total:,} reviews sampled")
    return _uniq_cap(why, 5), _uniq_cap(likes, 5)

def chunk_texts(texts, max_chars=1800):
    chunks, buf = [], ""
    for t in texts:
        if len(buf) + len(t) + 1 > max_chars:
            if buf: chunks.append(buf); buf = ""
        buf += ("\n" + t) if buf else t
    if buf: chunks.append(buf)
    return chunks

def get_or_make_summary(appid: int, short_desc: str, review_desc: Optional[str], total_reviews: Optional[int]):
    """Return {'why': [...], 'likes': [...]} with caching and robust fallbacks."""
    p = SUM_CACHE / f"{appid}.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and ("why" in data or "likes" in data):
                return data
        except Exception:
            pass

    # If no token, skip LLM and fall back immediately
    if not HF_TOKEN:
        why_f, likes_f = _fallback_from_metadata(short_desc, review_desc, total_reviews)
        result = {"why": why_f, "likes": likes_f}
        p.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"[summaries] no HF token; wrote fallback for {appid}")
        return result

    # Fetch recent reviews (small sample)
    reviews = fetch_review_texts(appid, num=30)
    if not reviews:
        why_f, likes_f = _fallback_from_metadata(short_desc, review_desc, total_reviews)
        result = {"why": why_f, "likes": likes_f}
        p.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"[summaries] no reviews; wrote fallback for {appid}")
        return result

    chunks = chunk_texts(reviews, max_chars=1800)

    why_all, likes_all = [], []
    for c in chunks:
        # Try strict JSON first (good with instruction models)
        prompt_json = (
            "You are a neutral game critic who aggregates player reviews into concise insights.\n"
            "Return ONLY valid JSON exactly in this schema:\n"
            '{"why":["..."],"likes":["..."]}\n'
            "Rules: max 5 items per array; each item <= 12 words; no duplicate ideas.\n"
            "Text:\n" + c
        )
        out = hf_generate(prompt_json)
        why, likes = _parse_json_like(out)

        if not (why or likes):
            # Try BART-friendly two-line format
            prompt_lines = (
                "From these player reviews, write two lines.\n"
                "Line 1 begins with 'WHY:' and lists up to 5 short phrases separated by semicolons.\n"
                "Line 2 begins with 'LIKES:' and lists up to 5 short phrases separated by semicolons.\n"
                "Keep each phrase under 12 words. No extra commentary.\n"
                "Text:\n" + c
            )
            out = hf_generate(prompt_lines)
            why, likes = _parse_two_lines(out)

        if why:  why_all.extend(why)
        if likes: likes_all.extend(likes)

    if not (why_all or likes_all):
        why_all, likes_all = _fallback_from_metadata(short_desc, review_desc, total_reviews)
        print(f"[summaries] fell back to metadata for {appid}")
    else:
        print(f"[summaries] generated bullets for {appid}: "
              f"{len(_uniq_cap(why_all,5))}/{len(_uniq_cap(likes_all,5))}")

    result = {"why": _uniq_cap(why_all, 5), "likes": _uniq_cap(likes_all, 5)}
    try:
        p.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception:
        pass
    return result

# =========================
# Main
# =========================
def main():
    now_utc   = dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    slug_ts   = now_local.strftime("%Y-%m-%d-%H%M%S")
    post_path = POST_DIR / f"{slug_ts}-auto.md"

    seen = load_seen()
    seen_set = set(seen)

    try:
        apps = get_applist()
    except Exception as e:
        print(f"[warn] GetAppList failed: {e}")
        apps = []

    appid, data = pick_game(apps, seen_set) if apps else (None, None)

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
        print(f"[ok] wrote fallback {post_path}")
        return

    # ----- Extract display fields -----
    name    = data.get("name", f"App {appid}")
    short   = clean_text(data.get("short_description", ""))
    header  = data.get("header_image", "")
    release = (data.get("release_date") or {}).get("date", "—")
    genres  = ", ".join([g.get("description") for g in (data.get("genres") or []) if g.get("description")][:5]) or "—"
    is_free = data.get("is_free", False)
    price   = (data.get("price_overview") or {}).get("final_formatted")
    price_str = "Free to play" if is_free else (price or "Price varies")

    try:
        summary = get_review_summary(appid)
    except Exception:
        summary = {}
    desc  = summary.get("review_score_desc")
    total = summary.get("total_reviews")

    # ----- Summaries (LLM + fallbacks) -----
    summ = get_or_make_summary(appid, short, desc, total)
    why_block = ("\n\n### Why it’s a hidden gem\n" +
                 "".join(f"- {w}\n" for w in (summ.get("why") or []))) if summ.get("why") else ""
    likes_block = ("\n\n### What players like\n" +
                   "".join(f"- {l}\n" for l in (summ.get("likes") or []))) if summ.get("likes") else ""

    # ----- Write post -----
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
    main()
