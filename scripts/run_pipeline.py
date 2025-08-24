#!/usr/bin/env python3
import datetime as dt
import json
import random
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

LOCAL_TZ = ZoneInfo("Europe/Berlin")   # change if you want
POST_DIR = Path("content/posts")
POST_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HiddenGemGamesBot/1.0 (+github actions)"})


def get_applist():
    """Fetch the full Steam app list (no key needed)."""
    url = "https://api.steampowered.com/ISteamApps/GetAppList/v2"
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("applist", {}).get("apps", [])


def get_appdetails(appid: int):
    """Fetch store metadata for one appid (undocumented, widely used)."""
    url = "https://store.steampowered.com/api/appdetails"
    r = SESSION.get(url, params={"appids": appid}, timeout=30)
    r.raise_for_status()
    j = r.json()
    item = j.get(str(appid))
    if not item or not item.get("success"):
        return None
    return item.get("data")


def get_review_summary(appid: int, lang="english"):
    """
    Get the aggregate review summary (percent positive, totals).
    We only need the 'query_summary' blob—cheap and small.
    """
    url = f"https://store.steampowered.com/appreviews/{appid}"
    params = {
        "json": 1,
        "language": lang,
        "purchase_type": "all",
        "filter": "summary",  # summary only
        "num_per_page": 1,    # minimal body
    }
    r = SESSION.get(url, params=params, timeout=30)
    r.raise_for_status()
    j = r.json()
    return j.get("query_summary", {}) if isinstance(j, dict) else {}


def clean_text(s: str, max_len=240):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[: max_len - 1] + "…" if len(s) > max_len else s


def pick_random_game(applist, max_tries=40):
    """Pick a random real GAME (not DLC/soundtrack/coming soon)."""
    for _ in range(max_tries):
        app = random.choice(applist)
        appid = app.get("appid")
        if not appid:
            continue
        data = get_appdetails(appid)
        if not data:
            continue
        if data.get("type") != "game":
            continue
        # skip unreleased/coming soon & missing basics
        rd = data.get("release_date", {})
        if rd.get("coming_soon"):
            continue
        name = data.get("name")
        header = data.get("header_image")
        if not name or not header:
            continue
        return appid, data
    return None, None


def build_markdown(now_local, appid, data, summary):
    name = data.get("name", f"App {appid}")
    short = clean_text(data.get("short_description", ""))
    is_free = data.get("is_free", False)
    price = None
    if data.get("price_overview"):
        price = f"{data['price_overview'].get('final_formatted')}"
    price_str = "Free to play" if is_free else (price or "Price varies")

    genres = ", ".join([g.get("description") for g in (data.get("genres") or [])][:5]) or "—"
    release = (data.get("release_date", {}) or {}).get("date", "—")
    header = data.get("header_image")
    link = f"https://store.steampowered.com/app/{appid}/"

    q = summary or {}
    percent = q.get("review_score")  # (1..9) Steam bucket, not %
    desc = q.get("review_score_desc")  # e.g., "Very Positive"
    total = q.get("total_reviews")

    # Make a friendly review line
    review_line = "Reviews: —"
    if desc and total:
        review_line = f"Reviews: **{desc}** ({total:,} total)"
    elif desc:
        review_line = f"Reviews: **{desc}**"

    # Compose markdown
    human = now_local.strftime("%Y-%m-%d %H:%M:%S %Z")
    slug_ts = now_local.strftime("%Y-%m-%d-%H%M%S")

    md = f"""Title: Hourly Game — {human}
Date: {now_local.strftime("%Y-%m-%d %H:%M")}
Category: Games
Tags: auto, steam
Slug: game-{slug_ts}

![{name}]({header})

**[{name}]({link})**

{short}

- {review_line}
- Release: **{release}**
- Genres: **{genres}**
- Price: **{price_str}**
- Steam AppID: `{appid}`

*This post was generated automatically. The game is randomly selected each run.*
"""
    return md


def main():
    # Use a stable seed per hour so retries pick the same game within an hour
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    seed = int(now_utc.strftime("%Y%m%d%H"))
    random.seed(seed)

    try:
        apps = get_applist()
    except Exception as e:
        # Fallback post on failure
        apps = []
        print(f"[warn] GetAppList failed: {e}")

    appid, data = pick_random_game(apps) if apps else (None, None)
    if not appid or not data:
        # Write a simple fallback post so the build doesn't fail
        fname = POST_DIR / f"{now_local.strftime('%Y-%m-%d-%H%M%S')}-auto.md"
        fname.write_text(
            f"""Title: Hourly Game — {now_local.strftime('%Y-%m-%d %H:%M %Z')}
Date: {now_local.strftime('%Y-%m-%d %H:%M')}
Category: Games
Tags: auto
Slug: fallback-{now_local.strftime('%Y-%m-%d-%H%M%S')}

Could not fetch Steam data this run. Will try again next hour.
""",
            encoding="utf-8",
        )
        print(f"[ok] wrote fallback {fname}")
        return

    # Review summary (non-fatal if it fails)
    summary = {}
    try:
        summary = get_review_summary(appid)
    except Exception as e:
        print(f"[warn] review summary failed for {appid}: {e}")

    # Write the post
    fname = POST_DIR / f"{now_local.strftime('%Y-%m-%d-%H%M%S')}-auto.md"
    md = build_markdown(now_local, appid, data, summary)
    fname.write_text(md, encoding="utf-8")
    print(f"[ok] wrote {fname} for appid {appid} — {data.get('name')!r}")


if __name__ == "__main__":
    main()
