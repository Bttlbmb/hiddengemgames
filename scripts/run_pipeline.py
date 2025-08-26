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
from typing import Optional, List, Dict
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter, Retry
from random import SystemRandom

# =========================
# Paths & constants
# =========================
LOCAL_TZ   = ZoneInfo("Europe/Berlin")
ROOT       = Path(".")
POST_DIR   = Path("content/posts")
DATA_DIR   = Path("content/data")
SUM_CACHE  = DATA_DIR / "summaries"
POOL_PATH  = DATA_DIR / "candidate_pool.json"
POOL_META  = DATA_DIR / "pool_meta.json"
APPSTATS_DIR = DATA_DIR / "appstats"
SEEN_PATH  = DATA_DIR / "seen.json"
APPLIST_CACHE = DATA_DIR / "applist.json"

for p in (POST_DIR, DATA_DIR, SUM_CACHE, APPSTATS_DIR):
    p.mkdir(parents=True, exist_ok=True)

# Harvest cadence / sizes
APPLIST_TTL_SECS   = 60 * 60 * 24 * 7  # refresh applist weekly
POOL_TTL_SECS      = 60 * 60 * 24 * 7  # refresh pool weekly
POOL_MIN_SIZE      = int(os.getenv("POOL_MIN_SIZE", "80"))  # keep at least N cached candidates
HARVEST_MAX_PROBE  = int(os.getenv("HARVEST_MAX_PROBE", "180"))  # probe up to N apps per harvest
HARVEST_FORCE      = os.getenv("HARVEST_FORCE") == "1"

# Hidden-gem hard gates
MIN_REVIEWS        = 50
MAX_REVIEWS        = 2000
MIN_POS_RATIO      = 0.85  # lifetime positivity if recent unavailable

# Diversity windows
NO_REPEAT_GENRE_DAYS    = 5
NO_REPEAT_PUBLISHER_DAYS= 14

# Networking: polite session
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "HiddenGemGamesBot/1.0 (contact: bot@example.invalid)"
})
retries = Retry(
    total=5, connect=3, read=3,
    backoff_factor=0.8,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=["GET", "HEAD"]
)
SESSION.mount("https://", HTTPAdapter(max_retries=retries))

MIN_INTERVAL_S = 0.75
_last_call = 0.0
rng = SystemRandom()

def _rate_limit():
    global _last_call
    now = time.monotonic()
    wait = _last_call + MIN_INTERVAL_S - now
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()

# =========================
# Small utilities
# =========================
def clean_text(s: str, max_len=240) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[: max_len - 1] + "…" if len(s) > max_len else s

def clamp_chars(s: str, max_len=650) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[: max_len].rstrip()

def load_seen(max_keep=500) -> List[int]:
    if SEEN_PATH.exists():
        try:
            data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
            return (data.get("seen_appids") or [])[-max_keep:]
        except Exception:
            return []
    return []

def save_seen(seen: List[int]):
    try:
        SEEN_PATH.write_text(json.dumps({"seen_appids": seen[-500:]}, indent=2), encoding="utf-8")
    except Exception:
        pass

def save_json(path: Path, obj):
    try:
        path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    except Exception:
        pass

def read_json(path: Path, default=None):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default

# =========================
# Steam API helpers (with caching)
# =========================
def _read_applist_cache():
    try:
        if APPLIST_CACHE.exists():
            age = time.time() - APPLIST_CACHE.stat().st_mtime
            data = json.loads(APPLIST_CACHE.read_text(encoding="utf-8"))
            return data, age
    except Exception:
        return None, None
    return None, None

def _write_applist_cache(apps):
    save_json(APPLIST_CACHE, apps)

def get_applist() -> List[Dict]:
    # Try network
    try:
        _rate_limit()
        r = SESSION.get("https://api.steampowered.com/ISteamApps/GetAppList/v2", timeout=30)
        r.raise_for_status()
        apps = (r.json().get("applist", {}) or {}).get("apps", [])
        if apps:
            _write_applist_cache(apps)
            return apps
    except Exception as e:
        print(f"[warn] GetAppList network failed: {e}")

    # Fallback to cache (even if stale)
    cached, age = _read_applist_cache()
    if cached:
        print(f"[info] Using cached app list (age ~{int(age)}s)")
        return cached
    return []

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

def fetch_review_texts(appid: int, num=20) -> List[str]:
    """Small page for LLM summaries. Keep num small to save requests."""
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
# Hidden-gem filters & GemScore (cache-first)
# =========================
NAME_BLOCKLIST = {
    "hentai","sex","nsfw","adult","ahega","porn","erotic","nudity","strip",
    "yuri","yaoi"
}

def english_supported(data: dict) -> bool:
    langs = data.get("supported_languages") or ""
    return ("English" in langs) if isinstance(langs, str) else True

def is_core_safe(d: dict) -> bool:
    if not d or d.get("type") != "game": return False
    if (d.get("release_date") or {}).get("coming_soon"): return False
    if not d.get("name") or not d.get("header_image"): return False
    if isinstance(d.get("supported_languages"), str) and "English" not in d["supported_languages"]: return False
    nm = (d.get("name") or "").lower()
    if any(b in nm for b in NAME_BLOCKLIST): return False
    ids = (d.get("content_descriptors") or {}).get("ids") or []
    if any(x in ids for x in (1,3)): return False
    if any(t in nm for t in ("demo","soundtrack","dlc","server","ost")): return False
    return True

def cache_appstats(appid, details, summary) -> dict:
    data = {
        "appid": appid,
        "name": details.get("name"),
        "genres": [g.get("description") for g in (details.get("genres") or []) if g.get("description")],
        "is_free": details.get("is_free", False),
        "price": (details.get("price_overview") or {}).get("final_formatted"),
        "release_date": (details.get("release_date") or {}).get("date"),
        "publisher": (details.get("publishers") or [None])[0],
        "header_image": details.get("header_image"),
        "total_reviews": summary.get("total_reviews", 0),
        "total_positive": summary.get("total_positive", 0),
        "review_score_desc": summary.get("review_score_desc"),
        "ts": int(time.time()),
    }
    save_json(APPSTATS_DIR / f"{appid}.json", data)
    return data

def read_appstats(appid) -> Optional[dict]:
    return read_json(APPSTATS_DIR / f"{appid}.json")

def refresh_candidate_pool(sample_appids: List[int], max_probe=HARVEST_MAX_PROBE) -> int:
    """Probe unseen appids, apply harsh gates, cache survivors → POOL_PATH."""
    added = 0
    pool = read_json(POOL_PATH, default=[]) or []
    present = {p["appid"] for p in pool}
    probed = 0
    for appid in sample_appids:
        if probed >= max_probe: break
        probed += 1
        if appid in present: continue

        try:
            details = get_appdetails(appid)
        except Exception:
            continue
        if not is_core_safe(details):
            continue

        try:
            summary = get_review_summary(appid)
        except Exception:
            continue

        total = int(summary.get("total_reviews", 0) or 0)
        if total < MIN_REVIEWS or total > MAX_REVIEWS:
            continue

        pos_ratio = (summary.get("total_positive", 0) / max(1, total))
        if pos_ratio < MIN_POS_RATIO:
            continue

        stats = cache_appstats(appid, details, summary)
        pool.append({k: stats.get(k) for k in (
            "appid","name","genres","is_free","price","header_image",
            "total_reviews","total_positive","review_score_desc",
            "release_date","publisher"
        )})
        added += 1

    if added:
        save_json(POOL_PATH, pool)
        save_json(POOL_META, {"last_refresh": int(time.time()), "size": len(pool)})
        print(f"[harvest] added {added} candidates (pool size={len(pool)})")
    else:
        print("[harvest] no new candidates added")
    return added

def pool_is_stale() -> bool:
    meta = read_json(POOL_META, {})
    if not meta: return True
    last = meta.get("last_refresh", 0)
    return (time.time() - last) > POOL_TTL_SECS

def applist_is_stale() -> bool:
    if not APPLIST_CACHE.exists(): return True
    age = time.time() - APPLIST_CACHE.stat().st_mtime
    return age > APPLIST_TTL_SECS

def try_harvest_if_needed():
    pool = read_json(POOL_PATH, default=[]) or []
    need = HARVEST_FORCE or pool_is_stale() or (len(pool) < POOL_MIN_SIZE)
    if not need:
        print(f"[harvest] skipped (pool ok, size={len(pool)})")
        return

    apps = []
    if applist_is_stale():
        apps = get_applist()
    else:
        apps, _age = _read_applist_cache()

    if not apps:
        print("[harvest] no applist available; aborting refresh")
        return

    rng.shuffle(apps)
    # probe across catalog; take a wide sample
    sample_ids = [a["appid"] for a in apps[: max(HARVEST_MAX_PROBE * 4, 400)]]
    refresh_candidate_pool(sample_ids, max_probe=HARVEST_MAX_PROBE)

# GemScore (from cached stats only)
def gemscore_from_cached(c: dict) -> float:
    total = max(1, int(c.get("total_reviews", 0) or 0))
    pos = (c.get("total_positive", 0) / total)
    # obscurity: 50→1.0, 2000→0.0
    try:
        obsc = 1 - (math.log10(total) - math.log10(MIN_REVIEWS)) / (math.log10(MAX_REVIEWS) - math.log10(MIN_REVIEWS))
    except Exception:
        obsc = 0.5
    obsc = max(0.0, min(1.0, obsc))
    # crude uniqueness by genre rarity -> fewer genres used = slightly higher
    genres = c.get("genres") or []
    uniq = 1.0 - min(1.0, len(genres)/8.0)
    # value proxy — unknown price treated neutral
    value = 0.6
    score = 100 * (0.45*pos + 0.25*obsc + 0.15*uniq + 0.15*value)
    return round(score, 1)

# Diversity constraints using seen history + metadata dates
def ok_with_diversity(c: dict, seen_ids: List[int], history_days=30) -> bool:
    # very light: block repeating primary genre and publisher from recent posts
    # (We don't have dates for seen.json items; it's just ids. So we only prevent immediate repeats.)
    # Improve: store history file with {appid, date, primary_genre, publisher}
    return True  # keep simple for now; you can wire a richer history later

# =========================
# Cloudflare Workers AI helpers (paragraphs)
# =========================
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_API_TOKEN  = os.getenv("CF_API_TOKEN")
CF_MODEL = "@cf/meta/llama-3-8b-instruct"

def cf_generate(prompt: str, max_tokens: int = 360) -> str:
    if not (CF_ACCOUNT_ID and CF_API_TOKEN and prompt.strip()):
        return ""
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    payload = {
        "messages": [
            {"role": "system", "content": "Return only the requested text. No JSON, no markdown, no preface."},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens
    }
    try:
        r = SESSION.post(url, headers=headers, json=payload, timeout=45)
        if r.status_code in (429, 503):
            return ""
        r.raise_for_status()
        j = r.json()
        return (j.get("result") or {}).get("response", "") or ""
    except Exception:
        return ""

# Local extractive helpers (fallback)
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
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if 6 <= len(p.strip()) <= 220]

def _tokens(text: str):
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP and len(t) > 2]

def _score_sentences(sentences):
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
        if any(w in low for w in _POS_WORDS): score *= 1.15
        if any(w in low for w in _FEAT_WORDS): score *= 1.10
        scores.append(score)
    return scores

def reviews_to_paragraph(reviews: list[str], want=3) -> Optional[str]:
    sents = []
    for rv in reviews:
        sents.extend(_sentences(rv))
    if not sents: return None
    scores = _score_sentences(sents)
    ranked = [s for _, s in sorted(zip(scores, sents), key=lambda x: x[0], reverse=True)]
    band = set(ranked[: max(5, want * 2)])
    top = []
    for s in sents:
        if s in band and s not in top:
            top.append(s)
        if len(top) >= want: break
    para = " ".join(top[:want])
    return clamp_chars(para, 650)

MARKETING_PAT = re.compile(
    r"\b(amazing|incredible|awesome|ultimate|epic|jaw[- ]?dropping|must[- ]?play|"
    r"groundbreaking|revolutionary|stunning|breathtaking)\b", re.I
)
def demarket(s: str) -> str:
    s = re.sub(MARKETING_PAT, "", s or "")
    s = re.sub(r"[!]{2,}", "!", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -–—")

# Paragraphs (CF → local → metadata) with caching
def cache_read_text(path: Path) -> Optional[str]:
    if path.exists():
        try:
            t = path.read_text(encoding="utf-8").strip()
            if t: return t
        except Exception:
            return None
    return None

def cache_write_text(path: Path, text: str):
    try:
        path.write_text(text, encoding="utf-8")
    except Exception:
        pass

def get_overview_paragraph(appid: int, name: str, short_desc: str, reviews: list[str]) -> str:
    path = SUM_CACHE / f"{appid}_overview.txt"
    cached = cache_read_text(path)
    if cached: return cached

    base = demarket(short_desc or "")
    sample = "\n\n".join(reviews[:15]) if reviews else ""
    if CF_ACCOUNT_ID and CF_API_TOKEN and (base or sample):
        prompt = (
            "Write a neutral, non-marketing overview of this PC game in 2–4 sentences. "
            "Avoid hype; describe premise, mechanics, and tone succinctly.\n"
            f"STEAM SHORT DESCRIPTION:\n{base}\n\n"
            f"REVIEWS SAMPLE:\n{sample}"
        )
        raw = cf_generate(prompt, max_tokens=420)
        if raw:
            text = clamp_chars(raw, 650)
            cache_write_text(path, text)
            print(f"[overview] CF")
            return text

    # local blend
    parts = []
    sents = _sentences(base)
    if sents: parts.append(" ".join(sents[:2]))
    add = reviews_to_paragraph(reviews, want=2) if reviews else None
    if add: parts.append(add)
    text = clamp_chars(" ".join(parts) or "A concise, neutral overview is unavailable for this title.", 650)
    cache_write_text(path, text)
    print(f"[overview] local")
    return text

def get_direct_summary(appid: int, name: str, reviews: list[str], mode: str) -> str:
    path = SUM_CACHE / f"{appid}_{mode}.txt"
    cached = cache_read_text(path)
    if cached: return cached

    if reviews and CF_ACCOUNT_ID and CF_API_TOKEN:
        sample = "\n\n".join(reviews[:20])
        if mode == "likes":
            prompt = (
                f"Summarize what is praised about the game {name} in 2–3 sentences. "
                "Use a direct, factual tone (e.g. 'Combat feels satisfying', 'Levels are well designed'). "
                "Do not hedge with 'players say' or 'some think'.\n"
                f"REVIEWS SAMPLE:\n{sample}"
            )
        else:
            prompt = (
                f"Summarize what is criticized about the game {name} in 2–3 sentences. "
                "Use a direct, factual tone (e.g. 'Controls are clunky', 'Performance is unstable'). "
                "Do not hedge with 'players say' or 'some think'.\n"
                f"REVIEWS SAMPLE:\n{sample}"
            )
        raw = cf_generate(prompt, max_tokens=360)
        if raw:
            text = clamp_chars(raw, 550)
            cache_write_text(path, text)
            print(f"[{mode}] CF")
            return text

    # local fallback
    para = reviews_to_paragraph(reviews, want=3) if reviews else None
    text = clamp_chars(para or ("Praised aspects not available." if mode=="likes" else "Criticized aspects not available."), 550)
    cache_write_text(path, text)
    print(f"[{mode}] local")
    return text

def get_hidden_gem_paragraph(appid: int, name: str, reviews: list[str],
                             review_desc: Optional[str], total_reviews: Optional[int]) -> str:
    path = SUM_CACHE / f"{appid}_hidden_gem.txt"
    cached = cache_read_text(path)
    if cached: return cached

    if reviews and CF_ACCOUNT_ID and CF_API_TOKEN:
        sig = []
        if total_reviews is not None: sig.append(f"total_reviews={total_reviews}")
        if review_desc: sig.append(f"review_score={review_desc}")
        sample = "\n\n".join(reviews[:20])
        prompt = (
            f"Explain in 1–2 sentences why {name} qualifies as a hidden gem. "
            "Highlight uniqueness, craft, or depth despite limited attention. "
            "Use a direct, matter-of-fact tone without hype.\n"
            f"Signals: {', '.join(sig) or 'n/a'}\n"
            f"REVIEWS SAMPLE:\n{sample}"
        )
        raw = cf_generate(prompt, max_tokens=220)
        if raw:
            text = clamp_chars(raw, 320)
            cache_write_text(path, text)
            print("[hidden_gem] CF")
            return text

    # heuristic fallback
    kw = re.compile(r"\b(hidden gem|underrated|overlooked|sleeper|surprised me|better than it looks)\b", re.I)
    hits = []
    for rv in (reviews or []):
        if kw.search(rv or ""):
            sents = _sentences(rv)
            if sents: hits.append(sents[0])
            if len(hits) >= 2: break
    parts = []
    if total_reviews is not None and review_desc and total_reviews < 500 and "Positive" in review_desc:
        parts.append(f"Strong {review_desc.lower()} reception despite a relatively small player base.")
    if hits:
        parts.append(clamp_chars(" ".join(hits[:1]), 200))
    if not parts and review_desc:
        parts.append(f"Its {review_desc.lower()} reviews suggest quality that hasn’t reached a wide audience.")
    text = clamp_chars(" ".join(parts) or "Well-regarded qualities with limited visibility make it easy to miss.", 320)
    cache_write_text(path, text)
    print("[hidden_gem] local")
    return text

# =========================
# Main (weekly harvest + daily pick with 0–1 Steam calls)
# =========================
def main():
    now_utc   = dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    slug_ts   = now_local.strftime("%Y-%m-%d-%H%M%S")
    post_path = POST_DIR / f"{slug_ts}-auto.md"

    # 1) Ensure we have a healthy pool (weekly harvest / on-demand)
    try_harvest_if_needed()

    # 2) Load pool; if still too small, do a mini top-up
    pool = read_json(POOL_PATH, default=[]) or []
    if len(pool) < max(20, POOL_MIN_SIZE//2):
        apps, _ = _read_applist_cache()
        if apps:
            rng.shuffle(apps)
            sample_ids = [a["appid"] for a in apps[:120]]
            refresh_candidate_pool(sample_ids, max_probe=min(60, HARVEST_MAX_PROBE))
            pool = read_json(POOL_PATH, default=[]) or []

    if not pool:
        # nothing to pick; write a friendly fallback post
        post_path.write_text(
            f"""Title: No Pick — {now_local.strftime('%Y-%m-%d %H:%M %Z')}
Date: {now_local.strftime('%Y-%m-%d %H:%M')}
Category: Games
Tags: auto
Slug: fallback-{slug_ts}

Candidate pool is empty right now. Will refresh and try again on the next run.
""",
            encoding="utf-8",
        )
        print("[pool] empty; wrote fallback")
        return

    # 3) Choose best candidate by GemScore (cached stats only)
    seen = load_seen()
    # Light diversity hook (placeholder -> always True). Extend if you keep richer history.
    candidates = [c for c in pool if ok_with_diversity(c, seen)]
    if not candidates:
        candidates = pool

    for c in candidates:
        c["gemscore"] = gemscore_from_cached(c)

    candidates.sort(key=lambda x: x["gemscore"], reverse=True)
    pick = candidates[0]
    appid = int(pick["appid"])
    name  = pick.get("name", f"App {appid}")

    # 4) Fetch small review sample for summaries (single Steam request today)
    reviews = fetch_review_texts(appid, num=20)

    # 5) Stats for info box
    total = int(pick.get("total_reviews") or 0)
    pos   = int(pick.get("total_positive") or 0)
    desc  = pick.get("review_score_desc")
    if desc and total:
        reviews_line = f"- Reviews: **{desc}** ({total:,} total; {round(100*pos/max(1,total))}%)"
    elif desc:
        reviews_line = f"- Reviews: **{desc}**"
    else:
        reviews_line = "- Reviews: —"

    # 6) Read details from cached appstats (no network)
    stats = read_appstats(appid) or pick
    header  = stats.get("header_image", "")
    release = stats.get("release_date", "—")
    genres  = ", ".join((stats.get("genres") or [])[:5]) or "—"
    price_str = "Free to play" if stats.get("is_free") else (stats.get("price") or "Price varies")

    # 7) Pull short description (only if we already have details cached; otherwise skip extra call)
    short_desc = ""
    # Try to read from cached details (already stored in appstats? If not, skip.)
    # We stored only minimal fields; short description wasn't cached to keep it light.
    # We'll avoid an extra request; the Overview will rely on reviews mostly.

    # 8) Generate paragraphs (CF → local → metadata)
    overview_text   = get_overview_paragraph(appid, name, short_desc, reviews)
    likes_text      = get_direct_summary(appid, name, reviews, mode="likes")
    dislikes_text   = get_direct_summary(appid, name, reviews, mode="dislikes")
    hidden_gem_text = get_hidden_gem_paragraph(appid, name, reviews, desc, total)

    # 9) Compose post
    md = f"""Title: {name}
Date: {now_local.strftime('%Y-%m-%d %H:%M')}
Category: Games
Tags: auto, steam
Slug: {appid}-{slug_ts}
Cover: {header}

![{name}]({header})

{reviews_line}
- Release: **{release}**
- Genres: **{genres}**
- Price: **{price_str}**
- Steam AppID: `{appid}`
- GemScore: **{candidates[0]['gemscore']}**

*Auto-generated; daily pick from a cached candidate pool refreshed weekly.*

### Overview

{overview_text}

### Why it’s a hidden gem

{hidden_gem_text}

### What players like

{likes_text}

### What players don’t like

{dislikes_text}
"""

    post_path.write_text(md, encoding="utf-8")
    print(f"[ok] wrote {post_path} — {name!r} (appid={appid})")

    # 10) Update seen history
    seen.append(appid)
    save_seen(seen)

if __name__ == "__main__":
    main()
