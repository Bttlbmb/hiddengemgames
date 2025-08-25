#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import datetime as dt
import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from random import SystemRandom

# =========================
# Paths & constants
# =========================
LOCAL_TZ  = ZoneInfo("Europe/Berlin")
POST_DIR  = Path("content/posts")
DATA_DIR  = Path("content/data")
SEEN_PATH = DATA_DIR / "seen.json"
SUM_CACHE = DATA_DIR / "summaries"

for p in (POST_DIR, DATA_DIR, SUM_CACHE):
    p.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HiddenGemGamesBot/1.0 (+github actions)"})


# Small rate limit to be kind to Steam
MIN_INTERVAL_S = 0.40
_last_call = 0.0
def _rate_limit():
    global _last_call
    now = time.monotonic()
    wait = _last_call + MIN_INTERVAL_S - now
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


rng = SystemRandom()


# =========================
# Utilities
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
    try:
        SEEN_PATH.write_text(json.dumps({"seen_appids": seen[-500:]}, indent=2), encoding="utf-8")
    except Exception:
        pass


# =========================
# Steam API helpers
# =========================
def get_applist():
    _rate_limit()
    r = SESSION.get("https://api.steampowered.com/ISteamApps/GetAppList/v2", timeout=30)
    r.raise_for_status()
    return r.json().get("applist", {}).get("apps", [])


def get_appdetails(appid: int):
    _rate_limit()
    r = SESSION.get("https://store.steampowered.com/api/appdetails",
                    params={"appids": appid}, timeout=30)
    r.raise_for_status()
    j = r.json()
    item = j.get(str(appid)) or {}
    return item.get("data") if item.get("success") else None


def get_review_summary(appid: int):
    _rate_limit()
    r = SESSION.get(
        f"https://store.steampowered.com/appreviews/{appid}",
        params={"json": 1, "language": "english", "purchase_type": "all",
                "filter": "summary", "num_per_page": 1},
        timeout=30,
    )
    r.raise_for_status()
    return (r.json() or {}).get("query_summary", {}) or {}


def fetch_review_texts(appid: int, num=40):
    """Fetch up to `num` recent review texts for an app."""
    out = []
    cursor = "*"
    while len(out) < num:
        _rate_limit()
        r = SESSION.get(
            f"https://store.steampowered.com/appreviews/{appid}",
            params={
                "json": 1, "language": "english", "purchase_type": "all",
                "filter": "recent", "num_per_page": 20, "cursor": cursor,
            },
            timeout=30,
        )
        if r.status_code != 200:
            break
        j = r.json()
        reviews = j.get("reviews") or []
        if not reviews:
            break
        for rv in reviews:
            txt = (rv.get("review") or "").strip()
            if txt:
                out.append(txt)
            if len(out) >= num:
                break
        cursor = j.get("cursor")
        if not cursor:
            break
    return out[:num]


# =========================
# Game picker with basic safety filters
# =========================
NAME_BLOCKLIST = {
    "hentai","sex","nsfw","adult","ahega","porn","erotic","nudity","strip",
    "yuri","yaoi"
}

def english_supported(data: dict) -> bool:
    langs = data.get("supported_languages") or ""
    return ("English" in langs) if isinstance(langs, str) else True


def is_allowed(data: dict) -> bool:
    if not data or data.get("type") != "game":
        return False
    if (data.get("release_date") or {}).get("coming_soon"):
        return False
    if not data.get("name") or not data.get("header_image"):
        return False
    if not english_supported(data):
        return False

    nm = (data.get("name") or "").lower()
    if any(b in nm for b in NAME_BLOCKLIST):
        return False

    # Basic content descriptor check (1/3 = the usual adult content descriptors)
    ids = (data.get("content_descriptors") or {}).get("ids") or []
    if any(x in ids for x in (1, 3)):
        return False

    # Exclude obvious non-games by name suffix
    if any(t in nm for t in ("demo", "soundtrack", "dlc", "server", "ost")):
        return False

    return True


def pick_game(apps, seen_set, tries=200):
    for _ in range(tries):
        app = rng.choice(apps)
        appid = app.get("appid")
        if not appid or appid in seen_set:
            continue
        try:
            data = get_appdetails(appid)
        except Exception:
            continue
        if is_allowed(data):
            return appid, data
    return None, None


# =========================
# Cloudflare Workers AI + robust fallbacks
# =========================
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_TOKEN  = os.getenv("CF_API_TOKEN")
# Model: good quality/latency balance for short JSON outputs
CF_MODEL = "@cf/meta/llama-3-8b-instruct"

def cf_generate(prompt: str, max_tokens: int = 320) -> str:
    """Call Cloudflare Workers AI chat API and return raw text. '' on failure."""
    if not (CF_ACCOUNT_ID and CF_API_TOKEN and prompt.strip()):
        return ""
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    payload = {
        "messages": [
            {"role": "system", "content": "You are a concise assistant that returns valid JSON only."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=45)
        if r.status_code in (429, 503):
            return ""
        r.raise_for_status()
        j = r.json()
        return (j.get("result") or {}).get("response", "") or ""
    except Exception:
        return ""

def _cap(s: str, words=14):
    parts = s.split()
    return " ".join(parts[:words]) + ("…" if len(parts) > words else "")

def _parse_cf_json(text: str):
    """Try to extract {'why':[...], 'likes':[...]} from model output."""
    try:
        start = text.find("{"); end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            obj = json.loads(text[start:end+1])
        else:
            obj = json.loads(text)
    except Exception:
        return None, None
    why  = obj.get("why")  if isinstance(obj, dict) else None
    likes = obj.get("likes") if isinstance(obj, dict) else None
    why  = [str(x).strip() for x in (why or []) if str(x).strip()]
    likes = [str(x).strip() for x in (likes or []) if str(x).strip()]
    return (why or None), (likes or None)

def _prompt_for_reviews(reviews: list[str]) -> str:
    sample = "\n\n".join(reviews[:20])
    return (
        "You will read a sample of Steam user reviews for a PC game.\n"
        "Return STRICT JSON only, no prose, with this schema exactly:\n"
        '{ "why": [ "<short reason>", ... ], "likes": [ "<short thing players like>", ... ] }\n'
        "- Generate up to 5 bullets for each array.\n"
        "- Each bullet MUST be under 12 words, specific, and not generic praise.\n"
        "- Do not include quotes, markdown, or trailing punctuation in items.\n\n"
        "REVIEWS SAMPLE:\n" + sample
    )

# -------- local extractive fallbacks (no LLM) --------
_STOP = {
    "the","a","an","and","or","but","if","then","so","to","of","in","on","at","for","from","with","by","as",
    "is","are","was","were","be","been","being","it","its","this","that","these","those","you","your","i","we",
    "they","he","she","him","her","them","our","us","me","my","mine","yours","their","theirs","his","hers",
    "not","no","yes","very","really","just","also","too","still","again","more","most","much","many","few",
    "can","could","should","would","will","wont","won't","cant","can't","dont","don't","did","didn't","does",
    "doesn't","do","have","has","had","having","make","made","get","got","like","lot","lots","thing","things",
    "game","games","play","played","playing","player","players","steam","time","times","one","two","three",
    "bit","little"
}
_POS_WORDS = {
    "enjoy","love","fun","great","excellent","polished","smooth","addictive","awesome",
    "amazing","satisfying","beautiful","charming","relaxing","clever","smart","unique",
    "solid","well-made","well made","well-designed","well designed","responsive","tight"
}
_FEAT_WORDS = {
    "puzzle","story","narrative","soundtrack","music","art","graphics","pixel","combat","mechanics","controls",
    "co-op","coop","multiplayer","exploration","level design","progression","boss","mode","roguelike","deck",
    "cards","strategy","platformer","metroidvania","physics","builder","craft","quest","dialogue","voice acting"
}
def _sentences(text: str):
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if 6 <= len(p.strip()) <= 200]
def _tokens(text: str):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 2]
def _dedup(seq, key=lambda x: x.lower(), limit=None):
    seen, out = set(), []
    for x in seq:
        k = key(x)
        if k in seen: continue
        seen.add(k); out.append(x)
        if limit and len(out) >= limit: break
    return out
def bullets_from_reviews_direct(reviews: list[str], max_items=5):
    sents = []
    for rv in reviews:
        sents.extend(_sentences(rv))
    sents = _dedup(sents)
    likes, why = [], []
    for s in sents:
        low = s.lower()
        if any(w in low for w in _POS_WORDS): likes.append(_cap(s))
        elif any(w in low for w in _FEAT_WORDS): why.append(_cap(s))
        else:
            if len(why) < max_items: why.append(_cap(s))
        if len(likes) >= max_items and len(why) >= max_items: break
    return why[:max_items], likes[:max_items]

def _fallback_from_metadata(short_desc: str, review_desc: Optional[str], total: Optional[int]):
    why = []
    if short_desc:
        for s in _sentences(short_desc):
            why.append(_cap(s))
            if len(why) >= 3: break
    likes = []
    if review_desc: likes.append(f"{review_desc} overall sentiment")
    if total: likes.append(f"{total:,} reviews sampled")
    return why[:5], likes[:5]

def get_or_make_summary(appid: int, short_desc: str, review_desc: Optional[str], total_reviews: Optional[int]):
    """
    Try Cloudflare LLM first → local extractive → metadata.
    Caches to content/data/summaries/{appid}.json
    """
    path = SUM_CACHE / f"{appid}.json"
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and ("why" in cached or "likes" in cached):
                return cached
        except Exception:
            pass

    reviews = fetch_review_texts(appid, num=40)

    # 1) Cloudflare Workers AI (if creds + reviews available)
    if reviews and CF_ACCOUNT_ID and CF_API_TOKEN:
        prompt = _prompt_for_reviews(reviews)
        raw = cf_generate(prompt)
        why_cf, likes_cf = _parse_cf_json(raw)
        if why_cf or likes_cf:
            result = {"why": [_cap(x) for x in (why_cf or [])][:5],
                      "likes": [_cap(x) for x in (likes_cf or [])][:5]}
            try: path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            except Exception: pass
            print(f"[summaries] CF JSON used for {appid} (why={len(result['why'])}, likes={len(result['likes'])})")
            return result

    # 2) Local direct-from-reviews
    if reviews:
        why_d, likes_d = bullets_from_reviews_direct(reviews, max_items=5)
        if why_d or likes_d:
            result = {"why": why_d, "likes": likes_d}
            try: path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            except Exception: pass
            print(f"[summaries] local bullets for {appid} (why={len(why_d)}, likes={len(likes_d)})")
            return result

    # 3) Metadata fallback
    why_f, likes_f = _fallback_from_metadata(short_desc, review_desc, total_reviews)
    result = {"why": why_f, "likes": likes_f}
    try: path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception: pass
    print(f"[summaries] metadata fallback for {appid}")
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

    # review summary (sentiment / totals)
    try:
        summary = get_review_summary(appid)
    except Exception as e:
        print(f"[warn] review summary failed for {appid}: {e}")
        summary = {}

    name    = data.get("name", f"App {appid}")
    short   = clean_text(data.get("short_description", ""))
    header  = data.get("header_image", "")
    release = (data.get("release_date") or {}).get("date", "—")
    genres  = ", ".join([g.get("description") for g in (data.get("genres") or []) if g.get("description")][:5]) or "—"
    is_free = data.get("is_free", False)
    price   = (data.get("price_overview") or {}).get("final_formatted")
    price_str = "Free to play" if is_free else (price or "Price varies")

    link  = f"https://store.steampowered.com/app/{appid}/"
    desc  = summary.get("review_score_desc")
    total = summary.get("total_reviews")

    # Build reviews line cleanly (avoid template leakage)
    if desc and (total is not None):
        reviews_line = f"- Reviews: **{desc}** ({total:,} total)"
    elif desc:
        reviews_line = f"- Reviews: **{desc}**"
    else:
        reviews_line = "- Reviews: —"

    # Build “why/likes” bullets (CF → local → metadata)
    summ = get_or_make_summary(appid, short, desc, total)

    # Compose the post (title at top is the game name in your template)
    md = f"""Title: {name}
Date: {now_local.strftime('%Y-%m-%d %H:%M')}
Category: Games
Tags: auto, steam
Slug: {appid}-{slug_ts}
Cover: {header}

![{name}]({header})

{short}

{reviews_line}
- Release: **{release}**
- Genres: **{genres}**
- Price: **{price_str}**
- Steam AppID: `{appid}`

*Auto-generated; game chosen randomly each run, avoiding recent repeats.*
"""

    if summ.get("why"):
        md += "\n### Why it’s a hidden gem\n"
        for w in summ["why"]:
            md += f"- {w}\n"

    if summ.get("likes"):
        md += "\n### What players like\n"
        for l in summ["likes"]:
            md += f"- {l}\n"

    post_path.write_text(md, encoding="utf-8")
    print(f"[ok] wrote {post_path} for {appid} — {name!r}")

    seen.append(appid)
    save_seen(seen)


if __name__ == "__main__":
    main()
