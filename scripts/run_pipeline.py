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
SUM_CACHE = DATA_DIR / "summaries"  # we cache likes paragraphs here

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

    ids = (data.get("content_descriptors") or {}).get("ids") or []
    if any(x in ids for x in (1, 3)):  # typical adult descriptors
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
# Cloudflare Workers AI paragraph summarizer
# =========================
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_TOKEN  = os.getenv("CF_API_TOKEN")
CF_MODEL = "@cf/meta/llama-3-8b-instruct"   # good balance for short outputs

def cf_generate(prompt: str, max_tokens: int = 280) -> str:
    """Call Cloudflare Workers AI chat API and return raw text. '' on failure."""
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
# Local extractive fallback → 2–3 sentence paragraph
# =========================
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
    return [p.strip() for p in parts if 6 <= len(p.strip()) <= 220]

def _tokens(text: str):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 2]

def _score_sentences(sentences):
    # tiny tf-idf-ish
    docs = [set(_tokens(s)) for s in sentences]
    tf = Counter([tok for s in sentences for tok in _tokens(s)])
    df = Counter([tok for d in docs for tok in d])
    N = max(1, len(sentences))
    idf = {t: math.log((N + 1) / (1 + df[t])) + 1.0 for t in df}
    scores = []
    for s in sentences:
        toks = _tokens(s)
        score = sum(tf[t] * idf.get(t, 0.0) for t in toks)
        low = s.lower()
        if any(w in low for w in _POS_WORDS):  # small positive bias
            score *= 1.15
        if any(w in low for w in _FEAT_WORDS):
            score *= 1.10
        scores.append(score)
    return scores

def _pick_top_sentences(reviews: list[str], want=3):
    sents = []
    for rv in reviews:
        sents.extend(_sentences(rv))
    if not sents:
        return []
    scores = _score_sentences(sents)
    ranked = [s for _, s in sorted(zip(scores, sents), key=lambda x: x[0], reverse=True)]
    # keep order as they appeared but limit to top selections
    top = []
    seen = set()
    for s in sents:
        if s in ranked[: max(5, want * 2)] and s not in seen:
            top.append(s); seen.add(s)
        if len(top) >= want:
            break
    return top[:want]

def reviews_to_paragraph(reviews: list[str]) -> Optional[str]:
    picks = _pick_top_sentences(reviews, want=3)
    if not picks:
        return None
    # Make it flow a bit
    para = " ".join(picks)
    # Light cleanup
    para = re.sub(r'\s+', ' ', para).strip()
    return para


# =========================
# Likes paragraph (CF → local → metadata) + caching
# =========================
def get_or_make_likes_paragraph(appid: int, name: str, short_desc: str,
                                review_desc: Optional[str], total_reviews: Optional[int]) -> str:
    cache_path = SUM_CACHE / f"{appid}_likes.txt"
    if cache_path.exists():
        try:
            text = cache_path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except Exception:
            pass

    # fetch raw reviews (for CF + local)
    reviews = fetch_review_texts(appid, num=40)

    # 1) Cloudflare LLM: 2–3 sentences, positive focus
    if reviews and CF_ACCOUNT_ID and CF_API_TOKEN:
        sample = "\n\n".join(reviews[:20])
        prompt = (
            "You are summarizing Steam user reviews for a PC game.\n"
            "Write 2–3 sentences describing what players appreciate about this game.\n"
            "Be specific and concise; avoid generic praise and avoid negatives.\n"
            f"Game title: {name}\n\n"
            "REVIEWS SAMPLE:\n" + sample
        )
        raw = cf_generate(prompt)
        if raw:
            text = raw.strip()
            # Light guardrails: ensure it's 1–3 sentences and not too long
            sents = _sentences(text)
            if sents:
                text = " ".join(sents[:3])
                text = clean_text(text, max_len=550)
                try: cache_path.write_text(text, encoding="utf-8")
                except Exception: pass
                print(f"[likes] CF paragraph used for {appid} ({len(sents[:3])} sentences)")
                return text

    # 2) Local extractive fallback → 2–3 sentences
    if reviews:
        local = reviews_to_paragraph(reviews)
        if local:
            text = clean_text(local, max_len=550)
            try: cache_path.write_text(text, encoding="utf-8")
            except Exception: pass
            print(f"[likes] local paragraph used for {appid}")
            return text

    # 3) Metadata fallback
    bits = []
    if review_desc:
        bits.append(f"Players report **{review_desc.lower()}** overall sentiment.")
    if short_desc:
        bits.append(clean_text(short_desc, max_len=320))
    if total_reviews:
        bits.append(f"Based on {total_reviews:,} reviews.")
    text = " ".join(bits) or "Players appreciate aspects highlighted by recent reviews."
    try: cache_path.write_text(text, encoding="utf-8")
    except Exception: pass
    print(f"[likes] metadata fallback for {appid}")
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
        print(f"[ok] wrote fallback {post_path}")
        return

    # Steam review summary (for the little "Reviews: ..." line)
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

    # Reviews line
    if desc and (total is not None):
        reviews_line = f"- Reviews: **{desc}** ({total:,} total)"
    elif desc:
        reviews_line = f"- Reviews: **{desc}**"
    else:
        reviews_line = "- Reviews: —"

    # Build the 2–3 sentence paragraph
    likes_text = get_or_make_likes_paragraph(appid, name, short, desc, total)

    # Compose the post (your template uses the Title at top = game name)
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
"""

    post_path.write_text(md, encoding="utf-8")
    print(f"[ok] wrote {post_path} for {appid} — {name!r}")

    seen.append(appid)
    save_seen(seen)


if __name__ == "__main__":
    main()
