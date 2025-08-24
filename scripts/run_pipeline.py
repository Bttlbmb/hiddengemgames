#!/usr/bin/env python3
import datetime as dt
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from random import SystemRandom
rng = SystemRandom()  # better randomness, no seeding

LOCAL_TZ = ZoneInfo("Europe/Berlin")  # change as you like
POST_DIR = Path("content/posts")
DATA_DIR = Path("content/data")
SEEN_PATH = DATA_DIR / "seen.json"
POST_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "HiddenGemGamesBot/1.0 (+github actions)"})


def clean_text(s: str, max_len=240):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[: max_len - 1] + "…" if len(s) > max_len else s


def get_applist():
    r = SESSION.get("https://api.steampowered.com/ISteamApps/GetAppList/v2", timeout=30)
    r.raise_for_status()
    return r.json().get("applist", {}).get("apps", [])


def get_appdetails(appid: int):
    r = SESSION.get("https://store.steampowered.com/api/appdetails", params={"appids": appid}, timeout=30)
    r.raise_for_status()
    j = r.json()
    item = j.get(str(appid)) or {}
    return item.get("data") if item.get("success") else None


def get_review_summary(appid: int):
    r = SESSION.get(
        f"https://store.steampowered.com/appreviews/{appid}",
        params={"json": 1, "language": "english", "purchase_type": "all", "filter": "summary", "num_per_page": 1},
        timeout=30,
    )
    r.raise_for_status()
    return (r.json() or {}).get("query_summary", {}) or {}


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


def pick_game(apps, seen_set, tries=200):
    """Pick a random game not in the seen set, skipping DLC/coming soon."""
    for _ in range(tries):
        app = rng.choice(apps)
        appid = app.get("appid")
        if not appid or appid in seen_set:
            continue
        data = get_appdetails(appid)
        if not data or data.get("type") != "game":
            continue
        rd = data.get("release_date") or {}
        if rd.get("coming_soon"):
            continue
        if not data.get("name") or not data.get("header_image"):
            continue
        return appid, data
    return None, None


def main():
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_local = now_utc.astimezone(LOCAL_TZ)
    slug_ts = now_local.strftime("%Y-%m-%d-%H%M%S")
    post_path = POST_DIR / f"{slug_ts}-auto.md"

    # Load seen appids
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
            f"""Title: Hourly Game — {now_local.strftime('%Y-%m-%d %H:%M %Z')}
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

    # Get review summary (non-fatal)
    summary = {}
    try:
        summary = get_review_summary(appid)
    except Exception as e:
        print(f"[warn] review summary failed for {appid}: {e}")

    # Build article
    name = data.get("name", f"App {appid}")
    short = clean_text(data.get("short_description", ""))
    header = data.get("header_image", "")
    link = f"https://store.steampowered.com/app/{appid}/"
    release = (data.get("release_date") or {}).get("date", "—")
    genres = ", ".join([g.get("description") for g in (data.get("genres") or [])][:5]) or "—"
    is_free = data.get("is_free", False)
    price = (data.get("price_overview") or {}).get("final_formatted")
    price_str = "Free to play" if is_free else (price or "Price varies")

    desc = summary.get("review_score_desc")
    total = summary.get("total_reviews")
    review_line = f"Reviews: **{desc}** ({total:,} total)" if (desc and total) else (f"Reviews: **{desc}**" if desc else "Reviews: —")

    md = f"""Title: Hourly Game — {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}
Date: {now_local.strftime('%Y-%m-%d %H:%M')}
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

*Auto-generated; game chosen randomly each run, avoiding recent repeats.*
"""

    post_path.write_text(md, encoding="utf-8")
    print(f"[ok] wrote {post_path} for {appid} — {name!r}")

    # Update seen list and save
    seen.append(appid)
    save_seen(seen)


if __name__ == "__main__":
    main()
