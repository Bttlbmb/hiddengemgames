# app/steam.py
"""
Steam helpers for Hidden Gem Games with caching and careful rate limiting.

Public API (used by app/main.py):
- get_applist()
- get_appdetails(appid)                 # cached
- get_review_summary_safe(appid)        # cached
- get_review_snippets_safe(appid, max_items=20)
- build_candidate_pool(apps, min_reviews=30, block_nsfw=True, cap=None, sample_size=None, batch_size=None, wait_s=None)
- pick_from_pool(pool)

Strategy:
- Two-phase harvest: details -> quick filters -> review summary
- On-disk caching to avoid repeat hits (content/data/appstats, content/data/reviewsum)
- Strict per-minute rate gate + small per-run chunks to avoid 429s
"""
import os
import json
import time
import random
from random import SystemRandom
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from collections import deque

import requests

# ---------- Config / knobs ----------

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HiddenGemGames/1.0 (+https://example.com)"})

DATA_DIR = Path("content/data")
APPSTATS_DIR = DATA_DIR / "appstats"
REVIEWSUM_DIR = DATA_DIR / "reviewsum"

APPSTATS_DIR.mkdir(parents=True, exist_ok=True)
REVIEWSUM_DIR.mkdir(parents=True, exist_ok=True)

# Sample sizes / pacing
POOL_SAMPLE_CAP = 10_000           # max apps to sample from applist before phase filters
POOL_SUMMARY_CAP = 1200            # max review summaries to check per run (phase 2)
HARVEST_CHUNK   = 400              # safety slice per run after sampling
PAUSE           = 0.15             # tiny pause after individual requests

# Rate limiting (per-minute gate for steam endpoints we hit frequently)
REQS_PER_MIN = 60
_REQ_TIMES: deque[float] = deque(maxlen=REQS_PER_MIN)

rng = SystemRandom()


def _rate_gate():
    """Very simple per-minute request gate."""
    now = time.time()
    _REQ_TIMES.append(now)
    if len(_REQ_TIMES) == _REQ_TIMES.maxlen:
        # enforce that the first of the last N requests was >= 60s ago
        earliest = _REQ_TIMES[0]
        delta = now - earliest
        if delta < 60.0:
            sleep_for = 60.0 - delta
            # nudge a bit to avoid nudging right into boundary
            sleep_for = max(0.0, sleep_for) + 0.05
            time.sleep(sleep_for)

    # also a tiny random pause to de-sync with other runs
    time.sleep(PAUSE)


def _get(url: str, params: Optional[dict] = None, retries: int = 3, backoff: float = 0.7) -> Optional[dict]:
    """HTTP GET with small retry and our gate."""
    attempt = 0
    exc: Optional[Exception] = None
    while attempt <= retries:
        try:
            _rate_gate()
            res = SESSION.get(url, params=params, timeout=30)
            if res.status_code == 200:
                try:
                    return res.json()
                finally:
                    # tiny pause after success
                    time.sleep(PAUSE)
            # 429/5xx: back off
            if res.status_code in (429, 500, 502, 503, 504):
                attempt += 1
                sleep = 0.8 + attempt * 0.7 + random.random() * 0.3
                time.sleep(sleep)
            else:
                return None
        except Exception as e:  # network, JSON, etc.
            exc = e
            attempt += 1
            sleep = 0.6 + attempt * 0.5
            time.sleep(sleep)
    # give up
    return None


# ---------- Caching helpers ----------

def _read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
    tmp.replace(path)


# ---------- Public: applist ----------

def get_applist() -> List[Dict[str, Any]]:
    """Fetch the full Steam applist (id + name). Cached on disk."""
    path = DATA_DIR / "applist.json"
    cached = _read_json(path)
    if isinstance(cached, list) and cached:
        return cached  # already a list of dicts

    url = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
    data = _get(url)
    apps = (data or {}).get("applist", {}).get("apps", [])
    if isinstance(apps, list) and apps:
        _write_json(path, apps)
    return apps


# ---------- Public: appdetails (cached) ----------

def _appstats_path(appid: int) -> Path:
    return APPSTATS_DIR / f"{appid}.json"


def get_appdetails(appid: int) -> Optional[dict]:
    """Steam appdetails with aggressive on-disk caching."""
    path = _appstats_path(appid)
    cached = _read_json(path)
    if cached is not None:
        return cached

    url = "https://store.steampowered.com/api/appdetails"
    data = _get(url, params={"appids": appid})
    # Store raw; callers know how to navigate
    if data is not None:
        _write_json(path, data)
    return data


# ---------- Public: review summary + snippets (cached) ----------

def _reviewsum_path(appid: int) -> Path:
    return REVIEWSUM_DIR / f"{appid}.json"


def get_review_summary_safe(appid: int) -> Optional[dict]:
    """
    Returns Steam review summary (lifetime recent, counts) or None on error.
    Cached to disk.
    """
    path = _reviewsum_path(appid)
    cached = _read_json(path)
    if cached is not None:
        return cached

    url = "https://store.steampowered.com/appreviews/{appid}"
    params = {
        "json": 1,
        "filter": "summary",
        "language": "all",
        "purchase_type": "all",
        "day_range": 3650,  # lifetime-ish
    }
    data = _get(url.format(appid=appid), params=params)
    if data is not None:
        _write_json(path, data)
    return data


def get_review_snippets_safe(appid: int, max_items: int = 20) -> List[str]:
    """
    Fetch a few review snippets (recent) for a title. Best-effort.
    We keep this small; it’s mostly for UI flavor text / sanity checks.
    """
    url = "https://store.steampowered.com/appreviews/{appid}"
    params = {
        "json": 1,
        "filter": "recent",
        "language": "english",
        "purchase_type": "all",
        "num_per_page": min(100, max(10, int(max_items))),
    }
    data = _get(url.format(appid=appid), params=params)
    out: List[str] = []
    try:
        for r in (data or {}).get("reviews", [])[:max_items]:
            s = (r.get("review") or "").strip()
            if s:
                out.append(s)
    except Exception:
        pass
    return out


# ---------- Quick filters & thresholds ----------

def _unwrap_details(data: dict) -> Tuple[bool, dict]:
    """
    appdetails returns {"<appid>": {"success": true, "data": {...}}}
    Return (ok, payload)
    """
    try:
        key = next(iter(data.keys()))
        entry = data[key]
        return bool(entry.get("success")), (entry.get("data") or {})
    except Exception:
        return False, {}


def _is_viable_game(payload: dict) -> bool:
    """Rough filters to avoid tools, videos, DLC-only, etc."""
    t = (payload.get("type") or "").lower()
    genres = [g.get("description", "").lower() for g in payload.get("genres", []) if isinstance(g, dict)]
    categories = [c.get("description", "").lower() for c in payload.get("categories", []) if isinstance(c, dict)]

    if t not in ("game", "dlc"):
        return False
    bad_genres = {"accounting", "video production", "tutorial", "software"}
    if any(g in bad_genres for g in genres):
        return False
    bad_cats = {"video", "trailer", "software training"}
    if any(c in bad_cats for c in categories):
        return False
    return True


def _is_nsfw(payload: dict) -> bool:
    """Basic NSFW detection based on Steam flags/genres/categories."""
    # Normalize content descriptor IDs to lowercase strings
    ids_raw = (payload.get("content_descriptors") or {}).get("ids", [])
    if isinstance(ids_raw, (int, str)):
        ids_raw = [ids_raw]
    flags = {str(x).strip().lower() for x in ids_raw if x is not None}

    # Also look at genres/categories text
    genres_txt = " ".join(
        g.get("description", "") for g in (payload.get("genres") or []) if isinstance(g, dict)
    )
    cats_txt = " ".join(
        c.get("description", "") for c in (payload.get("categories") or []) if isinstance(c, dict)
    )
    text = f"{genres_txt} {cats_txt}".lower()

    nsfw_words = ("adult", "sexual", "sex", "nudity", "nsfw", "hentai", "porn")
    if any(w in text for w in nsfw_words):
        return True

    # Some stores use small integer codes for mature content
    if "2" in flags:
        return True

    return False



def _passes_review_threshold_cached(appid: int, min_reviews: int) -> Tuple[bool, Optional[dict]]:
    """Return (passes, payload) for the summary threshold."""
    data = get_review_summary_safe(appid)
    try:
        total = int((data or {}).get("query_summary", {}).get("total_reviews", 0))
    except Exception:
        total = 0
    return (total >= min_reviews), data


# ---------- Candidate pool (weekly) ----------

def build_candidate_pool(
    apps: List[Dict[str, Any]],
    *,
    min_reviews: int = 30,
    block_nsfw: bool = True,
    cap: Optional[int] = None,
    sample_size: Optional[int] = None,
    batch_size: Optional[int] = None,
    wait_s: Optional[float] = None,
) -> List[int]:
    if not apps:
        return []

    # Prefer `sample_size` over legacy `cap` when provided
    effective_cap = sample_size if (sample_size is not None) else cap
    cap_val = int(effective_cap or POOL_SAMPLE_CAP)

    # Random sample, then limit to a small chunk per run
    sample = random.sample(apps, k=min(cap_val, len(apps)))
    chunk = int(batch_size or HARVEST_CHUNK)
    sample = sample[:min(chunk, len(sample))]

    pool: List[int] = []
    viable_ids: List[int] = []        # <- keep viable survivors for fallback
    checked_summaries = 0

    for idx, app in enumerate(sample, 1):
        appid = app.get("appid")
        if not appid:
            continue

        # Details (cached)
        details = get_appdetails(appid)

        # UNWRAP the appdetails response before applying filters
        ok, payload = _unwrap_details(details or {})
        if not ok:
            continue

        # Quick filters
        if not _is_viable_game(payload):
            continue
        if block_nsfw and _is_nsfw(payload):
            continue

        # Keep track of viable survivors regardless of review threshold
        viable_ids.append(int(appid))

        # Only fetch summary for survivors, capped
        if checked_summaries < POOL_SUMMARY_CAP:
            passed, _summary = _passes_review_threshold_cached(appid, min_reviews)
            checked_summaries += 1
            if passed:
                pool.append(int(appid))

        # Gentle pacing every N items
        if idx % 40 == 0:
            time.sleep(float(wait_s) if (wait_s is not None) else 0.8)

    # Cold-start fallback: if nothing passed the review threshold in this small batch,
    # return the viable survivors so the pool is not empty.
    if not pool and viable_ids:
        pool = viable_ids[: min(100, len(viable_ids))]

    random.shuffle(pool)
    return pool



# ---------- Daily picker ----------

def _normalize_pool_to_appids(pool) -> List[int]:
    """
    Accepts:
      - list[int] of appids,
      - list[dict] with 'appid' or 'id'
      - dict with 'items': [...]
    Returns: list[int]
    """
    if isinstance(pool, dict):
        items = pool.get("items") or []
    elif isinstance(pool, list):
        items = pool
    else:
        items = []

    appids: list[int] = []
    seen = set()
    for it in items:
        if isinstance(it, dict):
            raw = it.get("appid", it.get("id"))
        else:
            raw = it
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        if aid not in seen:
            seen.add(aid)
            appids.append(aid)
    return appids


def _weight_for_app(appid: int) -> float:
    """
    Optionally derive a weight from cached review stats (more reviews => slightly lower weight
    to bias toward smaller-but-viable titles). If we don't have data, return a small default.
    """
    data = get_review_summary_safe(appid) or {}
    try:
        total = int((data.get("query_summary") or {}).get("total_reviews", 0))
    except Exception:
        total = 0
    if total <= 0:
        return 1.0
    # simple inverse square root scaling
    return max(0.01, 1.0 / (total ** 0.5))


def pick_from_pool(
    pool,
    *,
    use_weights: bool = True,
    exclude: Optional[List[int]] = None,
) -> int:
    """
    Pick an appid from a harvested pool (daily choice).
    - pool can be a list[int], list[dict], or a dict with items:[]
    - exclude: appids to avoid this run (best effort; if it empties the pool we ignore it)
    """
    # Normalize the pool into a list of appids
    normalized = _normalize_pool_to_appids(pool)
    if not normalized:
        raise ValueError("Candidate pool empty after normalization.")

    # Apply exclusion but don't let it empty the pool
    exclude_set = set(exclude or [])
    candidates = [aid for aid in normalized if aid not in exclude_set]
    if not candidates:
        # All candidates were excluded; fall back to using the full pool
        # so the daily job still produces a post instead of failing.
        candidates = normalized

    # Optionally compute weights (cheap: only for the first 500 candidates)
    weight_map = {}
    if use_weights:
        for aid in candidates[:500]:
            weight_map[aid] = _weight_for_app(aid)

    if use_weights and weight_map:
        weights = [weight_map.get(aid, 0.01) for aid in candidates]
        import random
        return random.choices(candidates, weights=weights, k=1)[0]

    # Fallback: uniform pick
    return rng.choice(candidates)

