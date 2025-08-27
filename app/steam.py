# app/steam.py
"""
Steam helpers for Hidden Gem Games with caching and careful rate limiting.

Public API (used by app/main.py):
- get_applist()
- get_appdetails(appid)                 # cached
- get_review_summary_safe(appid)        # cached
- get_review_snippets_safe(appid, max_items=20)
- build_candidate_pool(apps, min_reviews=30, block_nsfw=True, cap=None)
- pick_from_pool(pool)

Strategy:
- Two-phase harvest: details -> quick filters -> review summary
- On-disk caching to avoid repeat hits (content/data/appstats, content/data/reviewsum)
- Strict per-minute rate gate + small per-run chunks to avoid 429s
- Retry & backoff for 429/5xx
"""

from __future__ import annotations

import os
import json
import time
import random
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from collections import deque

import requests

# ---------- Config / knobs ----------

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HiddenGemGamesBot/1.0 (+github actions)"})

TIMEOUT = float(os.environ.get("HGG_STEAM_TIMEOUT", "20"))
RETRIES = int(os.environ.get("HGG_STEAM_RETRIES", "3"))            # includes 429-aware retry
PAUSE   = float(os.environ.get("HGG_STEAM_PAUSE", "0.25"))         # small delay after successful call

# How many appids we sample *before* chunking
POOL_SAMPLE_CAP   = int(os.environ.get("HGG_POOL_SAMPLE_CAP", "600"))
# How many review summaries we fetch per run (upper bound)
POOL_SUMMARY_CAP  = int(os.environ.get("HGG_POOL_SUMMARY_CAP", "220"))
# How many review snippets to pull for LLM context when needed
REVIEWS_SNIPPET_CAP = int(os.environ.get("HGG_REVIEWS_SNIPPET_CAP", "20"))

# Process only a small slice per harvest run (keeps bursts tiny; cache builds over runs)
HARVEST_CHUNK = int(os.environ.get("HGG_HARVEST_CHUNK", "60"))

# Cache freshness (seconds)
DETAILS_TTL = int(os.environ.get("HGG_DETAILS_TTL", str(7 * 24 * 3600)))   # 7 days
SUMMARY_TTL = int(os.environ.get("HGG_SUMMARY_TTL", str(7 * 24 * 3600)))   # 7 days

# Per-minute global budget to stay comfortably under Steam limits
MAX_REQ_PER_MIN = int(os.environ.get("HGG_MAX_REQ_PER_MIN", "15"))

# Cache dirs
APPSTATS_DIR  = Path("content/data/appstats")
REVIEWSUM_DIR = Path("content/data/reviewsum")
APPSTATS_DIR.mkdir(parents=True, exist_ok=True)
REVIEWSUM_DIR.mkdir(parents=True, exist_ok=True)


# ---------- Rate limiting / HTTP helpers ----------

# Sliding window of recent request timestamps
_REQ_TIMES = deque(maxlen=MAX_REQ_PER_MIN * 2)

def _rate_gate():
    """
    Blocks if we've made >= MAX_REQ_PER_MIN requests in the last 60 seconds.
    Keeps us well below Steam's burst limits.
    """
    now = time.time()
    while _REQ_TIMES and (now - _REQ_TIMES[0]) > 60:
        _REQ_TIMES.popleft()

    if len(_REQ_TIMES) >= MAX_REQ_PER_MIN:
        sleep_for = 60 - (now - _REQ_TIMES[0]) + 0.05
        if sleep_for > 0:
            time.sleep(sleep_for)

    _REQ_TIMES.append(time.time())

def _should_retry(status: int) -> bool:
    # Retry for 429 and transient 5xx
    return status == 429 or 500 <= status < 600

def _get(url: str, *, params: Optional[dict] = None) -> requests.Response:
    last_exc = None
    for attempt in range(RETRIES + 1):
        try:
            _rate_gate()  # enforce per-minute budget
            r = SESSION.get(url, params=params, timeout=TIMEOUT)

            if _should_retry(r.status_code):
                # backoff with jitter to be polite
                sleep = 0.8 + attempt * 0.7 + random.random() * 0.3
                time.sleep(sleep)
                last_exc = requests.HTTPError(f"{r.status_code} {r.reason}")
            else:
                r.raise_for_status()
                # tiny pause after success
                time.sleep(PAUSE)
                return r
        except requests.RequestException as e:
            last_exc = e
            sleep = 0.6 + attempt * 0.5
            time.sleep(sleep)
    raise last_exc or RuntimeError("Steam request failed")


# ---------- Cache helpers ----------

def _cache_load(path: Path, ttl: int) -> Optional[dict]:
    try:
        if path.exists():
            age = time.time() - path.stat().st_mtime
            if age <= ttl:
                return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None

def _cache_save(path: Path, obj: dict) -> None:
    try:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=0), encoding="utf-8")
    except Exception:
        pass


# ---------- Core fetchers (cached) ----------

def get_applist() -> List[Dict[str, Any]]:
    """Full Steam applist (cache for a day)."""
    cache_path = Path("content/data/applist.json")
    cached = _cache_load(cache_path, ttl=24 * 3600)
    if cached and isinstance(cached, list):
        return cached
    r = _get("https://api.steampowered.com/ISteamApps/GetAppList/v2")
    apps = r.json().get("applist", {}).get("apps", []) or []
    _cache_save(cache_path, apps)
    return apps

def get_appdetails(appid: int) -> Optional[Dict[str, Any]]:
    """Cached appdetails from store.steampowered.com/api/appdetails."""
    p = APPSTATS_DIR / f"{appid}.json"
    cached = _cache_load(p, ttl=DETAILS_TTL)
    if cached is not None:
        return cached

    try:
        r = _get("https://store.steampowered.com/api/appdetails", params={"appids": appid})
        j = r.json()
        item = j.get(str(appid)) or {}
        if item.get("success"):
            data = item.get("data") or {}
            _cache_save(p, data)
            return data
    except Exception:
        pass
    return None

def get_review_summary_safe(appid: int) -> Dict[str, Any]:
    """Cached review summary; returns {} on failure."""
    p = REVIEWSUM_DIR / f"{appid}.json"
    cached = _cache_load(p, ttl=SUMMARY_TTL)
    if cached is not None:
        return cached
    try:
        r = _get(
            f"https://store.steampowered.com/appreviews/{appid}",
            params={
                "json": 1,
                "language": "english",
                "purchase_type": "all",
                "filter": "summary",
                "num_per_page": 1,
            },
        )
        data = (r.json() or {}).get("query_summary", {}) or {}
        _cache_save(p, data)
        return data
    except Exception:
        return {}

def get_review_snippets_safe(appid: int, max_items: int = REVIEWS_SNIPPET_CAP) -> List[str]:
    """Small set of review snippets (non-cached; optional for LLM context)."""
    try:
        r = _get(
            f"https://store.steampowered.com/appreviews/{appid}",
            params={
                "json": 1,
                "language": "english",
                "purchase_type": "all",
                "filter": "recent",
                "num_per_page": max(5, min(max_items, 40)),
            },
        )
        j = r.json() or {}
        revs = j.get("reviews") or []
        out: List[str] = []
        for rv in revs:
            txt = (rv.get("review") or "").strip()
            if txt:
                out.append(txt)
            if len(out) >= max_items:
                break
        return out
    except Exception:
        return []


# ---------- Filtering helpers ----------

_NSFW_HINTS = ("adult", "sexual", "nudity", "nsfw")

def _to_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def _is_nsfw(data: Dict[str, Any]) -> bool:
    """Heuristics for adult/NSFW content; robust to str/None values from Steam."""
    if not data:
        return False

    # required_age may be "18", 18, or None
    if _to_int(data.get("required_age"), 0) >= 18:
        return True

    # content_descriptors: ids + notes
    cd = (data.get("content_descriptors") or {})
    notes = (cd.get("notes") or "")
    if isinstance(notes, str) and any(k in notes.lower() for k in _NSFW_HINTS):
        return True

    ids = cd.get("ids") or []
    try:
        id_ints = {_to_int(i) for i in ids}
    except Exception:
        id_ints = set()
    # Steam often uses 1–4 for adult content flags
    if any(i in id_ints for i in (1, 2, 3, 4)):
        return True

    acd = (data.get("adult_content_description") or "")
    if isinstance(acd, str) and any(k in acd.lower() for k in _NSFW_HINTS):
        return True

    return False

def _is_viable_game(data: Dict[str, Any]) -> bool:
    if not data:
        return False
    if data.get("type") != "game":
        return False
    rd = data.get("release_date") or {}
    if rd.get("coming_soon"):
        return False
    if not data.get("name") or not data.get("header_image"):
        return False
    return True

def _passes_review_threshold_cached(appid: int, min_reviews: int) -> Tuple[bool, Dict[str, Any]]:
    summary = get_review_summary_safe(appid)
    total = _to_int(summary.get("total_reviews"), 0)
    return (total >= min_reviews), summary


# ---------- Candidate pool (weekly) ----------

def build_candidate_pool(
    apps: List[Dict[str, Any]],
    *,
    min_reviews: int = 30,
    block_nsfw: bool = True,
    cap: Optional[int] = None,
) -> List[int]:
    """
    Build a list of promising appids using a sampled subset of the applist.
    Uses caching + two-phase filtering to keep API calls low.

    Phase 1: get_appdetails(appid) -> _is_viable_game / NSFW filter
    Phase 2: get_review_summary_safe(appid) for survivors up to POOL_SUMMARY_CAP
    """
    if not apps:
        return []

    cap = int(cap or POOL_SAMPLE_CAP)
    sample = random.sample(apps, k=min(cap, len(apps)))
    # Only process a small chunk per run to avoid bursts & let cache accumulate.
    sample = sample[:min(HARVEST_CHUNK, len(sample))]

    pool: List[int] = []
    checked_summaries = 0

    for idx, app in enumerate(sample, 1):
        appid = app.get("appid")
        if not appid:
            continue

        # Details (cached)
        data = get_appdetails(appid)
        if not _is_viable_game(data or {}):
            continue
        if block_nsfw and _is_nsfw(data or {}):
            continue

        # Only fetch summary for survivors, capped
        if checked_summaries < POOL_SUMMARY_CAP:
            ok, _summary = _passes_review_threshold_cached(appid, min_reviews)
            checked_summaries += 1
            if ok:
                pool.append(int(appid))
        else:
            # cap reached; still add viable items (they passed basic viability)
            pool.append(int(appid))

        # Gentle pacing every N items
        if idx % 40 == 0:
            time.sleep(0.8)

    random.shuffle(pool)
    return pool


# ---------- Daily picker ----------

def pick_from_pool(pool: List[int]) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Choose a random id from the pool and fetch details for rendering."""
    if not pool:
        return None
    tries = min(10, len(pool))
    for _ in range(tries):
        appid = int(random.choice(pool))
        data = get_appdetails(appid)
        if _is_viable_game(data or {}):
            return appid, data  # type: ignore[return-value]
    return None
