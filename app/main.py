# app/main.py
"""
Entry points for Hidden Gem Games:
- --harvest : refresh weekly candidate pool (no deploy)
- --daily   : pick one game, generate a post
This version writes editorial paragraphs for likes/dislikes/overview (no bullet points).
"""

from __future__ import annotations

import os
import re
import json
import textwrap
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
from random import SystemRandom
from typing import Tuple, List

from . import steam
from . import storage
from .ai import (
    build_corpus,
    make_overview_text,
    make_hidden_gem_text,
    make_likes_text,
    make_dislikes_text,
)

LOCAL_TZ = ZoneInfo(os.environ.get("HGG_TZ", "Europe/Berlin"))
POST_DIR = Path("content/posts")
DATA_DIR = Path("content/data")
SUM_DIR  = DATA_DIR / "summaries"
POST_DIR.mkdir(parents=True, exist_ok=True)
SUM_DIR.mkdir(parents=True, exist_ok=True)

rng = SystemRandom()

# Toggle network fetch (Steam + AI). 0 => offline (use cached only)
FETCH_OK = os.environ.get("HGG_FETCH", "1") != "0"

# Basic thresholds (fast and safe)
MIN_REVIEWS = int(os.environ.get("HGG_MIN_REVIEWS", "30"))
MIN_STEAM_SCORE = float(os.environ.get("HGG_MIN_STEAM_SCORE", "70"))  # derived % if you store one
DISALLOW_NSFW = os.environ.get("HGG_BLOCK_NSFW", "1") == "1"


def _clean(s: str, n=400) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[: n - 1] + "…" if len(s) > n else s


def _save_md(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    print(f"[ok] wrote {path}")


def _sample_reviews_text(reviews: List[str], cap_chars=800) -> str:
    """Collapse a small handful of reviews into a plain text block, capped by characters."""
    out: List[str] = []
    total = 0
    for r in reviews:
        r = _clean(r, 300)
        if not r:
            continue
        if total + len(r) + 1 > cap_chars:
            break
        out.append(r)
        total += len(r) + 1
    return "\n".join(out)


def _md_from_paras(title: str, header_img: str, link: str, meta_block: str,
                   overview: str, why: str, likes: str, dislikes: str) -> str:
    # Paragraph-only sections (no lists), tidy headings.
    body = textwrap.dedent(f"""
    Title: {title}
    Date: {dt.datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')}
    Category: Games
    Tags: auto, steam
    Slug: gem-{dt.datetime.now(LOCAL_TZ).strftime('%Y%m%d%H%M%S')}
    Cover: {header_img}

    ![]({header_img})

    {meta_block}

    ### Overview
    {overview}

    ### Why it’s a hidden gem
    {why}

    ### What players like
    {likes}

    ### What players don’t like
    {dislikes}

    *Auto-generated; daily pick from a cached candidate pool refreshed weekly.*
    """).strip()
    return body + "\n"


# -------------------- DAILY PICK --------------------

def run_daily() -> None:
    # pick from candidate pool (weekly refreshed)
    pool = storage.load_candidate_pool()
    if not pool:
        print("[warn] candidate pool empty; you may need to run --harvest")
        apps = steam.get_applist() if FETCH_OK else []
        picked = steam.fast_pick(apps, min_reviews=MIN_REVIEWS, block_nsfw=DISALLOW_NSFW) if apps else None
    else:
        picked = steam.pick_from_pool(pool)

    if not picked:
        # fallback post
        now_local = dt.datetime.now(LOCAL_TZ)
        slug_ts = now_local.strftime("%Y-%m-%d-%H%M%S")
        _save_md(
            POST_DIR / f"{slug_ts}-auto.md",
            textwrap.dedent(f"""\
                Title: Hourly Game — {now_local.strftime('%Y-%m-%d %H:%M %Z')}
                Date: {now_local.strftime('%Y-%m-%d %H:%M')}
                Category: Games
                Tags: auto
                Slug: fallback-{slug_ts}

                Could not fetch Steam data this run. Will try again next time.
            """),
        )
        return

    appid, data = picked
    name = data.get("name", f"App {appid}")
    header = data.get("header_image", "")
    link = f"https://store.steampowered.com/app/{appid}/"
    short = _clean(data.get("short_description", ""), 500)

    # Minimal meta for the top block
    summary = steam.get_review_summary_safe(appid) if FETCH_OK else {}
    desc = summary.get("review_score_desc")
    total = summary.get("total_reviews")
    review_line = f"Reviews: **{desc}** ({total:,} total)" if (desc and total) else (f"Reviews: **{desc}**" if desc else "Reviews: —")

    release = (data.get("release_date") or {}).get("date", "—")
    genres = ", ".join([g.get("description") for g in (data.get("genres") or [])][:5]) or "—"
    is_free = data.get("is_free", False)
    price = (data.get("price_overview") or {}).get("final_formatted")
    price_str = "Free to play" if is_free else (price or "Price varies")

    gemscore = storage.estimate_gemscore(appid, summary, data)  # your helper; OK if it returns None
    gemline = f"\n- GemScore: **{gemscore:.1f}**" if isinstance(gemscore, (int, float)) else ""

    meta_block = textwrap.dedent(f"""
    **[{name}]({link})**

    - {review_line}
    - Release: **{release}**
    - Genres: **{genres}**
    - Price: **{price_str}**
    - Steam AppID: `{appid}`{gemline}
    """).strip()

    # ---------- Editorial paragraphs ----------
    # Build a tiny corpus: description + a handful of review snippets
    reviews_snips = []
    if FETCH_OK:
        reviews_snips = steam.get_review_snippets_safe(appid, max_items=20) or []
    corpus = build_corpus(short, _sample_reviews_text(reviews_snips, cap_chars=900))

    if FETCH_OK:
        try:
            overview = make_overview_text(corpus)
            why      = make_hidden_gem_text(corpus)
            likes    = make_likes_text(corpus)
            dislikes = make_dislikes_text(corpus)
        except Exception as e:
            print(f"[summaries] workers-ai failed: {e}; falling back to metadata.")
            overview = short or "Overview not available."
            why      = "Well-liked niche qualities and a focused premise can make this one stand out."
            likes    = "Players respond positively to its core loop and presentation."
            dislikes = "Criticism tends to focus on rough edges and limited depth."
    else:
        overview = short or "Overview not available."
        why      = "Well-liked niche qualities and a focused premise can make this one stand out."
        likes    = "Players respond positively to its core loop and presentation."
        dislikes = "Criticism tends to focus on rough edges and limited depth."

    # ---------- Write markdown ----------
    now_local = dt.datetime.now(LOCAL_TZ)
    slug_ts = now_local.strftime("%Y-%m-%d-%H%M%S")
    md = _md_from_paras(
        title=name,
        header_img=header,
        link=link,
        meta_block=meta_block,
        overview=overview,
        why=why,
        likes=likes,
        dislikes=dislikes,
    )
    _save_md(POST_DIR / f"{slug_ts}-auto.md", md)


# -------------------- WEEKLY HARVEST --------------------

def run_harvest() -> None:
    if not FETCH_OK:
        print("[harvest] HGG_FETCH=0 — skipping network harvest.")
        return
    print("[harvest] refreshing candidate pool…")
    apps = steam.get_applist()
    pool = steam.build_candidate_pool(apps, min_reviews=MIN_REVIEWS, block_nsfw=DISALLOW_NSFW)
    storage.save_candidate_pool(pool)
    print(f"[harvest] pool size: {len(pool)}")


# -------------------- CLI --------------------

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--harvest", action="store_true", help="refresh weekly candidate pool only")
    p.add_argument("--daily",   action="store_true", help="generate daily pick post")
    args = p.parse_args()

    if args.harvest:
        run_harvest()
    elif args.daily:
        run_daily()
    else:
        # default to daily
        run_daily()


if __name__ == "__main__":
    main()
