#!/usr/bin/env python3
# -*- coding: utf-8 -*-

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
SEEN_PATH = DATA_DIR / "seen.json"

POST_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HiddenGemGamesBot/1.0 (+github actions)"})

rng = SystemRandom()

# Hidden-gem heuristics
GOOD_REVIEW_DESC = {"Very Positive", "Overwhelmingly Positive", "Mostly Positive", "Positive"}
MAX_PRICE_CENTS = 2500  # $25

# -------- Speed toggle --------
FAST_MODE = True         # ⚡ faster build (fewer requests)
FAST_TRIES = 40          # max attempts in fast mode
FAST_PRICE_CENTS = 2500
# ------------------------------

# Rate limiting for Steam endpoints
MIN_INTERVAL_S = 0.45  # ~2 req/sec
_last_call = 0.0

# -------- Content safety filters --------
ADULT_KEYWORDS = {
    "nudity", "sexual", "sex", "adult", "hentai", "nsfw", "porn",
    "ecchi", "erotic", "lewd", "fetish"
}
JOKE_KEYWORDS = {"meme", "joke", "satire", "parody", "troll"}
NAME_BLOCKLIST = {
    "hentai", "sex", "nude", "adult", "nsfw", "porn", "strip",
    "ecchi", "erotic", "boobs", "yaoi", "yuri", "ahega", "ahegal"
}
# ---------------------------------------


# ---------- Utilities ----------
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


# ---------- HTTP with cache/backoff ----------
def _cache_path(kind: str, key: str) -> Path:
    return CACHE_DIR / f"{kind}_{key}.json"


def _cache_load(kind: str, key: str, max_age_days: int) -> Optional[Any]:
    p = _cache_path(kind, key)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            obj = json.load(f)
        ts = obj.get("_cached_at")
        if not ts:
            return None
        age_days = (time.time() - ts) / 86400.0
        if age_days > max_age_days:
            return None
        return obj.get("data")
    except Exception:
        return None


def _cache_store(kind: str, key: str, data: Any):
    p = _cache_path(kind, key)
    try:
        with p.open("w", encoding="utf-8") as f:
            json.dump({"_cached_at": time.time(), "data": data}, f)
    except Exception:
        pass


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
                raise requests.HTTPError(f"{r.status_code} {r.reason}", response=r)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                jitter = rng.uniform(0, 0.4)
                time.sleep(backoff + jitter)
                backoff *= 2.0
                continue
            raise


# ---------- Steam fetchers (cached) ----------
def get_applist():
    j = http_get_json("https://api.steampowered.com/ISteamApps/GetAppList/v2")
    return j.get("applist", {}).get("apps", [])


def get_appdetails(appid: int):
    cache_key = str(appid)
    cached = _cache_load("app", cache_key, max_age_days=30)
    if cached is not None:
        return cached

    j = http_get_json("https://store.steampowered.com/api/appdetails", params={"appids": appid})
    item = j.get(str(appid)) or {}
    data = item.get("data") if item.get("success") else None
    if data:
        _cache_store("app", cache_key, data)
    return data


def get_review_summary(appid: int):
    cache_key = str(appid)
    cached = _cache_load("review", cache_key, max_age_days=7)
    if cached is not None:
        return cached

    j = http_get_json(
        f"https://store.steampowered.com/appreviews/{appid}",
        params={"json": 1, "language": "english", "purchase_type": "all",
                "filter": "summary", "num_per_page": 1},
    )
    summary = (j or {}).get("query_summary", {}) or {}
    _cache_store("review", cache_key, summary)
    return summary


# ---------- Genre/tag safety helpers ----------
def _has_keyword_any(items, keywords) -> bool:
    for it in items:
        if any(k in (it or "").lower() for k in keywords):
            return True
    return False


def is_safe_genres(data: dict) -> bool:
    # Genres & categories arrays
    genre_desc = [g.get("description", "") for g in (data.get("genres") or [])]
    cat_desc = [c.get("description", "") for c in (data.get("categories") or [])]
    # Steam sometimes includes steamspy tags or tag strings
    spy = data.get("steamspy_tags") or []
    if isinstance(spy, dict):
        spy = list(spy.keys())

    # Content descriptors (may be dict with 'notes' string or list)
    cd = data.get("content_descriptors") or {}
    notes = cd.get("notes") if isinstance(cd, dict) else None
    if isinstance(notes, str):
        notes_list = [notes]
    elif isinstance(notes, list):
        notes_list = notes
    else:
        notes_list = []

    # Any adult keyword in these?
    if _has_keyword_any(genre_desc + cat_desc + spy + notes_list, ADULT_KEYWORDS):
        return False

    # Also exclude obvious meme/joke tags early
    if _has_keyword_any(genre_desc + cat_desc + spy, JOKE_KEYWORDS):
        return False

    return True


def is_not_meme(data: dict) -> bool:
    genre_desc = [g.get("description", "") for g in (data.get("genres") or [])]
    spy = data.get("steamspy_tags") or []
    if isinstance(spy, dict):
        spy = list(spy.keys())
    return not _has_keyword_any(genre_desc + spy, JOKE_KEYWORDS)


def is_clean_name(name: str) -> bool:
    low = (name or "").lower()
    return not any(bad in low for bad in NAME_BLOCKLIST)


# ---------- Picker helpers ----------
def parse_review_meta(summary: dict):
    total = summary.get("total_reviews")
    desc = summary.get("review_score_desc")
    try:
        total = int(total) if total is not None else None
    except Exception:
        total = None
    return total, desc


def extract_genres(data: dict) -> list[str]:
    return [g.get("description") for g in (data.get("genres") or []) if g.get("description")]


def read_recent_genres(n_posts=10) -> set[str]:
    try:
        posts = sorted(POST_DIR.glob("*.md"), reverse=True)[:n_posts]
        genres = set()
        for p in posts:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.lower().startswith("- genres:"):
                    parts = line.split("**")
                    if len(parts) >= 2:
                        for g in parts[1].split(","):
                            g = g.strip()
                            if g:
                                genres.add(g)
                    break
        return genres
    except Exception:
        return set()


# ---------- Candidate checks ----------
def is_basic_candidate(data: dict) -> bool:
    """Fast-mode check: type, not coming soon, has name/image, English, price ok, safe content."""
    if not data or data.get("type") != "game":
        return False
    if (data.get("release_date") or {}).get("coming_soon"):
        return False
    if not data.get("name") or not data.get("header_image"):
        return False
    # English?
    langs = data.get("supported_languages") or ""
    if isinstance(langs, str) and "English" not in langs:
        return False

    # Price gate
    is_free = data.get("is_free", False)
    price_cents = (data.get("price_overview") or {}).get("final")
    if not (is_free or (isinstance(price_cents, int) and price_cents <= FAST_PRICE_CENTS)):
        return False

    # Safety gates
    if not is_safe_genres(data):
        return False
    if not is_not_meme(data):
        return False
    if not is_clean_name(data.get("name", "")):
        return False

    # Quick name filter
    name = (data.get("name") or "").lower()
    if any(bad in name for bad in ("demo", "soundtrack", "ost", "dlc", "server")):
        return False

    return True


def is_hidden_gem_candidate(data: dict, review_summary: dict) -> bool:
    total, desc = parse_review_meta(review_summary)
    if desc and desc not in GOOD_REVIEW_DESC:
        return False
    if total is None or not (40 <= total <= 5000):
        return False
    return is_basic_candidate(data)


# ---------- Pickers ----------
def pick_game(apps, seen_set):
    if FAST_MODE:
        attempts = 0
        while attempts < FAST_TRIES:
            attempts += 1
            app = rng.choice(apps)
            appid = app.get("appid")
            if not appid or appid in seen_set:
                continue
            try:
                data = get_appdetails(appid)
            except Exception as e:
                print(f"[warn] appdetails {appid} failed: {e}")
                continue
            if not data:
                continue
            if is_basic_candidate(data):
                return appid, data
        print("[info] FAST_MODE found nothing; returning None")
        return None, None
    else:
        return pick_game_weighted(apps, seen_set)


def pick_game_weighted(apps, seen_set, tries=300):
    candidates = []
    recent_genres = read_recent_genres(n_posts=10)

    attempts = 0
    while attempts < tries and len(candidates) < 25:
        attempts += 1
        app = rng.choice(apps)
        appid = app.get("appid")
        if not appid or appid in seen_set:
            continue

        try:
            data = get_appdetails(appid)
        except Exception as e:
            print(f"[warn] appdetails {appid} failed: {e}")
            continue
        if not data:
            continue

        try:
            summary = get_review_summary(appid)
        except Exception as e:
            print(f"[warn] reviews {appid} failed: {e}")
            summary = {}

        if not is_hidden_gem_candidate(data, summary):
            continue

        # (You can reintroduce the detailed scoring here if you like.)
        candidates.append((appid, data))

    if not candidates:
        return None, None

    return rng.choice(candidates)


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
        print(f"[ok] wrote fallback {post_path}")
        return

    # Build article
    name = data.get("name", f"App {appid}")
    short = clean_text(data.get("short_description", ""))
    header = data.get("header_image", "")
    release = (data.get("release_date") or {}).get("date", "—")
    genres = ", ".join(extract_genres(data)[:5]) or "—"
    is_free = data.get("is_free", False)
    price = (data.get("price_overview") or {}).get("final_formatted")
    price_str = "Free to play" if is_free else (price or "Price varies")

    # Only one review summary call (for final pick)
    try:
        summary = get_review_summary(appid)
    except Exception as e:
        print(f"[warn] review summary failed for {appid}: {e}")
        summary = {}
    desc = summary.get("review_score_desc")
    total = summary.get("total_reviews")
    likes = f"{desc} — {total:,} reviews" if (desc and total) else (desc or None)

    g_list = extract_genres(data)
    why_bits = []
    if short:
        why_bits.append(short)
    if g_list:
        why_bits.append("Genres: " + ", ".join(g_list[:3]))
    why = clean_text(" — ".join(why_bits), max_len=200) if why_bits else None

    md = f"""Title: {name}
Date: {now_local.strftime('%Y-%m-%d %H:%M')}
Category: Games
Tags: auto, steam
Slug: {appid}-{slug_ts}
Cover: {header}
{"Why: " + why if why else ""}
{"Likes: " + likes if likes else ""}

![{name}]({header})

{short}

- Reviews: **{desc}**{f" ({total:,} total)" if total else "" if desc else ""}
- Release: **{release}**
- Genres: **{genres}**
- Price: **{price_str}**
- Steam AppID: `{appid}`

*Auto-generated; game chosen randomly each run, avoiding recent repeats.*
"""

    post_path.write_text(md, encoding="utf-8")
    print(f"[ok] wrote {post_path} for {appid} — {name!r}")

    seen.append(appid)
    save_seen(seen)


if __name__ == "__main__":
    main()
