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


# Be kind to Steam
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
    out = []
    cursor = "*"
    while len(out) < num:
        _rate_limit()
        r = SESSION.get(
            f"https://store.steampowered.com/appreviews/{appid}",
            params={"json": 1, "language": "english", "purchase_type": "all",
                    "filter": "recent", "num_per_page": 20, "cursor": cursor},
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
# Game picker
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

    ids = (data.get("content_descriptors") or {}).get("ids") or []
    if any(x in ids for x in (1, 3)):
        return False

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
# Cloudflare Workers AI
# =========================
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_TOKEN  = os.getenv("CF_API_TOKEN")
CF_MODEL = "@cf/meta/llama-3-8b-instruct"

def cf_generate(prompt: str, max_tokens: int = 280) -> str:
    if not (CF_ACCOUNT_ID and CF_API_TOKEN and prompt.strip()):
        return ""
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    payload = {
        "messages": [
            {"role": "system", "content": "You are concise and return only the requested text, no JSON."},
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


# =========================
# Summaries
# =========================
def get_summary_paragraph(appid: int, name: str, reviews: list[str], short_desc: str,
                          review_desc: Optional[str], total_reviews: Optional[int],
                          mode: str = "likes") -> str:
    """
    mode = "likes" → what players enjoy
    mode = "dislikes" → what players complain about
    """
    cache_path = SUM_CACHE / f"{appid}_{mode}.txt"
    if cache_path.exists():
        try:
            text = cache_path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except Exception:
            pass

    if reviews and CF_ACCOUNT_ID and CF_API_TOKEN:
        sample = "\n\n".join(reviews[:20])
        if mode == "likes":
            prompt = (
                f"You are summarizing Steam user reviews for the game **{name}**.\n"
                "Write 2–3 sentences describing what players like and appreciate about the game.\n"
                "Be concise, specific, and focus on positive aspects."
                f"\nREVIEWS SAMPLE:\n{sample}"
            )
        else:
            prompt = (
                f"You are summarizing Steam user reviews for the game **{name}**.\n"
                "Write 2–3 sentences describing what players often complain about or dislike.\n"
                "Be concise, specific, and focus on critical aspects."
                f"\nREVIEWS SAMPLE:\n{sample}"
            )
        raw = cf_generate(prompt)
        if raw:
            text = clean_text(raw, max_len=550)
            try: cache_path.write_text(text, encoding="utf-8")
            except Exception: pass
            return text

    # fallback
    if mode == "likes":
        bits = []
        if review_desc:
            bits.append(f"Players report **{review_desc.lower()}** overall sentiment.")
        if short_desc:
            bits.append(clean_text(short_desc, max_len=320))
        if total_reviews:
            bits.append(f"Based on {total_reviews:,} reviews.")
        text = " ".join(bits)
    else:
        text = "Some players mention rough edges or frustrations, but reviews vary."

    try: cache_path.write_text(text, encoding="utf-8")
    except Exception: pass
    return text


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
        return

    try:
        qsum = get_review_summary(appid)
    except Exception as e:
        print(f"[warn] review summary failed for {appid}: {e}")
        qsum = {}

    name    = data.get("name", f"App {appid}")
    short   = clean_text(data.get("short_description", ""))
    header  = data.get("header_image", "")
    release = (data.get("release_date") or {}).get("date", "—")
    genres  = ", ".join([g.get("description") for g in (data.get("genres") or []) if g.get("description")][:5]) or "—"
    is_free = data.get("is_free", False)
    price   = (data.get("price_overview") or {}).get("final_formatted")
    price_str = "Free to play" if is_free else (price or "Price varies")

    link  = f"https://store.steampowered.com/app/{appid}/"
    desc  = qsum.get("review_score_desc")
    total = qsum.get("total_reviews")

    if desc and (total is not None):
        reviews_line = f"- Reviews: **{desc}** ({total:,} total)"
    elif desc:
        reviews_line = f"- Reviews: **{desc}**"
    else:
        reviews_line = "- Reviews: —"

    # get reviews
    reviews = fetch_review_texts(appid, num=40)
    likes_text    = get_summary_paragraph(appid, name, reviews, short, desc, total, mode="likes")
    dislikes_text = get_summary_paragraph(appid, name, reviews, short, desc, total, mode="dislikes")

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

### What players like

{likes_text}

### What players don’t like

{dislikes_text}
"""

    post_path.write_text(md, encoding="utf-8")
    print(f"[ok] wrote {post_path} for {appid} — {name!r}")

    seen.append(appid)
    save_seen(seen)


if __name__ == "__main__":
    main()
