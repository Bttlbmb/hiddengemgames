# app/steam.py
"""
Steam helpers for Hidden Gem Games.

This module keeps API usage modest and provides:
- get_applist()
- get_appdetails(appid)
- get_review_summary_safe(appid)
- get_review_snippets_safe(appid, max_items=20)
- build_candidate_pool(apps, min_reviews=30, block_nsfw=True, cap=800)
- pick_from_pool(pool)

All network calls are wrapped with timeouts and light backoff.
"""

from __future__ import annotations

import os
import json
import time
import random
from typing import Dict, Any, List, Optional, Tuple

import requests

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HiddenGemGamesBot/1.0 (+github actions)"})

TIMEOUT = float(os.environ.get("HGG_STEAM_TIMEOUT", "20"))
RETRIES = int(os.environ.get("HGG_STEAM_RETRIES", "2"))

# Caps to avoid hammering Steam during harvest
POOL_SAMPLE_CAP = int(os.environ.get("HGG_POOL_SAMPLE_CAP", "800"))  # max apps to inspect per harvest
REVIEWS_SNIPPET_CAP = int(os.environ.get("HGG_REVIEWS_SNIPPET_CAP", "20"))


def _get(url: str, *, params: Optional[dict] = None) -> requests.Response:
    last = None
    for i in range(RETRIES + 1):
        try:
            r = SESSION.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            time.sleep(0.7 + 0.4 * i)
    raise last  # type: ignore[misc]


# ----------------------- Core data fetchers -----------------------

def get_applist() -> List[Dict[str, Any]]:
    """Full Steam applist (once per week in harvest)."""
    r = _get("https://api.steampowered.com/ISteamApps/GetAppList/v2")
    return r.json().get("applist", {}).get("apps", [])


def get_appdetails(appid: int) -> Optional[Dict[str, Any]]:
    """App details for a single appid."""
    r = _get(
        "https://store.steampowered.com/api/appdetails",
        params={"appids": appid},
    )
    j = r.json()
    item = j.get(str(appid)) or {}
    if item.get("success"):
        return item.get("data") or {}
    return None


def get_review_summary_safe(appid: int) -> Dict[str, Any]:
    """Query summary with minimal cost; return {} on failure."""
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
        return (r.json() or {}).get("query_summary", {}) or {}
    except Exception:
        return {}


def get_review_snippets_safe(appid: int, max_items: int = REVIEWS_SNIPPET_CAP) -> List[str]:
    """
    Pull a small page of reviews so we can build a lightweight corpus for LLM.
    We deliberately keep this tiny to conserve both Steam and AI quotas.
    """
    try:
        r = _get(
            f"https://store.steampowered.com/appreviews/{appid}",
            params={
                "json": 1,
                "language": "english",
                "purchase_type": "all",
                "filter": "recent",  # small bias toward recent wording
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


# ----------------------- Filtering helpers -----------------------

_NSFW_HINTS = ("adult", "sexual", "nudity", "nsfw")


def _to_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def _is_nsfw(data: Dict[str, Any]) -> bool:
    """Heuristics for adult/NSFW content based on Steam fields, robust to string/None values."""
    if not data:
        return False

    # required_age can be "0", "18", 0, 18, or None
    if _to_int(data.get("required_age"), 0) >= 18:
        return True

    # content_descriptors: notes (string) + ids (list[int|str])
    cd = (data.get("content_descriptors") or {})
    notes = (cd.get("notes") or "")
    if isinstance(notes, str) and any(k in notes.lower() for k in _NSFW_HINTS):
        return True

    ids = cd.get("ids") or []
    # Protect against strings: ["1","2"] etc.
    try:
        id_ints = {_to_int(i) for i in ids}
    except Exception:
        id_ints = set()
    # Steam commonly uses 1–4 for adult content flags
    if any(i in id_ints for i in (1, 2, 3, 4)):
        return True

    # adult_content_description may be a string
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


def _passes_review_threshold(appid: int, min_reviews: int) -> Tuple[bool, Dict[str, Any]]:
    summary = get_review_summary_safe(appid)
    total = int(summary.get("total_reviews") or 0)
    if total < min_reviews:
        return False, summary
    return True, summary


# ----------------------- Candidate pool (weekly) -----------------------

def build_candidate_pool(
    apps: List[Dict[str, Any]],
    *,
    min_reviews: int = 30,
    block_nsfw: bool = True,
    cap: Optional[int] = None,
) -> List[int]:
    """
    Build a list of promising appids using a random sample of the applist to stay under rate limits.

    Heuristics:
      - must be a 'game', released, has header image
      - (optional) not NSFW
      - at least min_reviews total reviews (from summary endpoint)

    Returns a list of appids.
    """
    if not apps:
        return []

    # Randomly sample the applist to ~cap apps to inspect in this harvest.
    cap = int(cap or POOL_SAMPLE_CAP)
    sample = random.sample(apps, k=min(cap, len(apps)))

    pool: List[int] = []
    for i, app in enumerate(sample, 1):
        appid = app.get("appid")
        if not appid:
            continue

        # Details
        data = get_appdetails(appid)
        if not _is_viable_game(data or {}):
            continue

        if block_nsfw and _is_nsfw(data or {}):
            continue

        ok, _summary = _passes_review_threshold(appid, min_reviews)
        if not ok:
            continue

        pool.append(int(appid))

        # Tiny pacing to be nice to Steam
        if i % 40 == 0:
            time.sleep(0.8)

    # Shuffle once more so daily picker isn’t biased by loop order
    random.shuffle(pool)
    return pool


# ----------------------- Daily picker -----------------------

def pick_from_pool(pool: List[int]) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Choose a random id from the pool and fetch details for rendering."""
    if not pool:
        return None
    for _ in range(min(8, len(pool))):
        appid = int(random.choice(pool))
        data = get_appdetails(appid)
        if _is_viable_game(data or {}):
            return appid, data  # type: ignore[return-value]
    return None
