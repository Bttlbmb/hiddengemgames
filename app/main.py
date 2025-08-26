#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import math
import random
from pathlib import Path
from typing import Dict, List, Optional

from . import config as C
from . import storage as S
from . import steam
from . import ai

rng = random.SystemRandom()

# ---- product rules (core filters & scoring) ----
NAME_BLOCKLIST = {"hentai","sex","nsfw","adult","ahega","porn","erotic","nudity","strip","yuri","yaoi"}

def english_supported(details: dict) -> bool:
    langs = details.get("supported_languages") or ""
    return ("English" in langs) if isinstance(langs, str) else True

def is_core_safe(details: dict) -> bool:
    if not details or details.get("type") != "game": return False
    if (details.get("release_date") or {}).get("coming_soon"): return False
    if not details.get("name") or not details.get("header_image"): return False
    if isinstance(details.get("supported_languages"), str) and "English" not in details["supported_languages"]: return False
    nm = (details.get("name") or "").lower()
    if any(b in nm for b in NAME_BLOCKLIST): return False
    ids = (details.get("content_descriptors") or {}).get("ids") or []
    if any(x in ids for x in (1,3)): return False
    if any(t in nm for t in ("demo","soundtrack","dlc","server","ost")): return False
    return True

def gemscore_from_cached(c: dict) -> float:
    total = max(1, int(c.get("total_reviews", 0) or 0))
    pos   = (c.get("total_positive", 0) / total)
    # obscurity: 50→1.0, 2000→0.0
    try:
        obsc = 1 - (math.log10(total) - math.log10(C.MIN_REVIEWS)) / (math.log10(C.MAX_REVIEWS) - math.log10(C.MIN_REVIEWS))
    except Exception:
        obsc = 0.5
    obsc = max(0.0, min(1.0, obsc))
    genres = c.get("genres") or []
    uniq = 1.0 - min(1.0, len(genres)/8.0)  # crude rarity proxy
    value = 0.6  # neutral until we compute playtime/price
    score = 100 * (0.45*pos + 0.25*obsc + 0.15*uniq + 0.15*value)
    return round(score, 1)

def ok_with_diversity(c: dict, seen_ids: List[int]) -> bool:
    # Placeholder: allow all; extend with real history if desired
    return True

# ---- harvesting (low request budget) ----
def _cache_appstats(appid: int, details: dict, summary: dict) -> dict:
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
        "ts": int(C.now_local().timestamp()),
    }
    S.write_appstats(appid, data)
    return data

def refresh_candidate_pool(sample_appids: List[int], max_probe: int) -> int:
    pool = S.read_pool()
    present = {p["appid"] for p in pool}
    added = 0
    probed = 0
    for appid in sample_appids:
        if probed >= max_probe: break
        probed += 1
        if appid in present: continue

        try:
            details = steam.get_appdetails(appid)
        except Exception:
            continue
        if not is_core_safe(details):
            continue

        try:
            summary = steam.get_review_summary(appid)
        except Exception:
            continue

        total = int(summary.get("total_reviews", 0) or 0)
        if total < C.MIN_REVIEWS or total > C.MAX_REVIEWS:
            continue
        pos_ratio = (summary.get("total_positive", 0) / max(1, total))
        if pos_ratio < C.MIN_POS_RATIO:
            continue

        stats = _cache_appstats(appid, details, summary)
        pool.append({k: stats.get(k) for k in (
            "appid","name","genres","is_free","price","header_image",
            "total_reviews","total_positive","review_score_desc",
            "release_date","publisher"
        )})
        added += 1

    if added:
        S.write_pool(pool)
        S.write_pool_meta({"last_refresh": int(C.now_local().timestamp()), "size": len(pool)})
        print(f"[harvest] added {added} (pool size={len(pool)})")
    else:
        print("[harvest] no new candidates")
    return added

def pool_is_stale() -> bool:
    meta = S.read_pool_meta()
    if not meta: return True
    last = meta.get("last_refresh", 0)
    import time
    return (time.time() - last) > C.POOL_TTL_SECS

def applist_is_stale() -> bool:
    mt = S.applist_mtime()
    if mt is None: return True
    return C.is_stale(mt, C.APPLIST_TTL_SECS)

def try_harvest_if_needed():
    pool = S.read_pool()
    need = C.HARVEST_FORCE or pool_is_stale() or (len(pool) < C.POOL_MIN_SIZE)
    if not need:
        print(f"[harvest] skipped (pool ok, size={len(pool)})")
        return

    apps = []
    if applist_is_stale():
        apps = steam.get_applist()
    else:
        apps = S.read_applist()

    if not apps:
        print("[harvest] no applist; aborting")
        return

    rng.shuffle(apps)
    sample_ids = [a["appid"] for a in apps[: max(C.HARVEST_MAX_PROBE * 4, 400)]]
    refresh_candidate_pool(sample_ids, max_probe=C.HARVEST_MAX_PROBE)

# ---- rendering ----
def _md_escape(s: str) -> str:
    return (s or "").replace("<", "&lt;").replace(">", "&gt;")

def render_post(appid: int, pick: dict, reviews: List[str]) -> str:
    name   = pick.get("name", f"App {appid}")
    stats  = S.read_appstats(appid) or pick
    header = stats.get("header_image", "")
    release = stats.get("release_date", "—")
    genres  = ", ".join((stats.get("genres") or [])[:5]) or "—"
    price_str = "Free to play" if stats.get("is_free") else (stats.get("price") or "Price varies")
    total = int(pick.get("total_reviews") or 0)
    pos   = int(pick.get("total_positive") or 0)
    desc  = pick.get("review_score_desc")
    if desc and total:
        reviews_line = f"- Reviews: **{desc}** ({total:,} total; {round(100*pos/max(1,total))}%)"
    elif desc:
        reviews_line = f"- Reviews: **{desc}**"
    else:
        reviews_line = "- Reviews: —"

    # For overview we don't fetch short_desc in daily mode to avoid extra request; pass empty.
    short_desc = ""

    # Paragraphs (respect offline toggle)
    if C.should_fetch():
        overview  = ai.make_overview(appid, name, short_desc, reviews)
        likes     = ai.make_likes(appid, name, reviews)
        dislikes  = ai.make_dislikes(appid, name, reviews)
        hidden_gem= ai.make_hidden_gem(appid, name, reviews, desc, total)
    else:
        overview   = S.read_summary(appid, "overview")   or "Overview unavailable (offline)."
        likes      = S.read_summary(appid, "likes")      or "Likes unavailable (offline)."
        dislikes   = S.read_summary(appid, "dislikes")   or "Dislikes unavailable (offline)."
        hidden_gem = S.read_summary(appid, "hidden_gem") or "Hidden-gem note unavailable (offline)."

    now = C.now_local()
    slug_ts = now.strftime("%Y-%m-%d-%H%M%S")
    md = f"""Title: {name}
Date: {now.strftime('%Y-%m-%d %H:%M')}
Category: Games
Tags: auto, steam
Slug: {appid}-{slug_ts}
Cover: {header}

![{_md_escape(name)}]({header})

{reviews_line}
- Release: **{_md_escape(release)}**
- Genres: **{_md_escape(genres)}**
- Price: **{_md_escape(price_str)}**
- Steam AppID: `{appid}`

*Auto-generated; daily pick from a cached candidate pool refreshed weekly.*

### Overview

{overview}

### Why it’s a hidden gem

{hidden_gem}

### What players like

{likes}

### What players don’t like

{dislikes}
"""
    return md

# ---- daily run (0–1 Steam calls)
def run_daily():
    # ensure pool healthy (but skip refresh in offline mode)
    if C.should_fetch():
        try_harvest_if_needed()
    pool = S.read_pool()
    if not pool:
        now = C.now_local()
        slug_ts = now.strftime("%Y-%m-%d-%H%M%S")
        post_path = C.POST_DIR / f"{slug_ts}-auto.md"
        post_path.write_text(
            f"""Title: No Pick — {now.strftime('%Y-%m-%d %H:%M %Z')}
Date: {now.strftime('%Y-%m-%d %H:%M')}
Category: Games
Tags: auto
Slug: fallback-{slug_ts}

Candidate pool is empty or offline mode is enabled. Will try again later.
""", encoding="utf-8")
        print("[daily] wrote fallback (empty pool)")
        return

    seen = S.read_seen()
    candidates = [c for c in pool if ok_with_diversity(c, seen)]
    if not candidates: candidates = pool
    for c in candidates:
        c["gemscore"] = gemscore_from_cached(c)
    candidates.sort(key=lambda x: x["gemscore"], reverse=True)
    pick = candidates[0]
    appid = int(pick["appid"])

    # fetch 1 page of reviews only if allowed
    if C.should_fetch():
        reviews = steam.fetch_review_texts(appid, num=20)
    else:
        reviews = []

    md = render_post(appid, pick, reviews)
    now = C.now_local()
    slug_ts = now.strftime("%Y-%m-%d-%H%M%S")
    post_path = C.POST_DIR / f"{slug_ts}-auto.md"
    post_path.write_text(md, encoding="utf-8")
    print(f"[daily] wrote {post_path} — {pick.get('name')!r}")

    seen.append(appid)
    S.write_seen(seen)

# ---- weekly harvest (low requests)
def run_harvest():
    # refresh applist if stale, do probes with harsh gates
    try_harvest_if_needed()

# ---- entrypoint
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", action="store_true", help="Run daily pick and write a post")
    ap.add_argument("--harvest", action="store_true", help="Refresh candidate pool (low network)")
    args = ap.parse_args()

    if args.harvest:
        if not C.should_fetch():
            print("[harvest] skipped (offline toggle is ON)")
            return
        run_harvest()
    else:
        run_daily()

if __name__ == "__main__":
    main()
